"""Checkpoint projection and orchestration for two-layer compaction."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mewcode_agent.compaction.estimator import ContextTokenEstimator
from mewcode_agent.compaction.models import (
    CompactionConfig,
    ContextCompactionError,
    ContextEstimate,
    ContextStatus,
    SummaryCheckpoint,
    SummarySections,
    ToolCompactionResult,
    VerbatimUserMessage,
)
from mewcode_agent.compaction.summarizer import ContextSummarizer
from mewcode_agent.compaction.tool_results import ToolResultCompactor
from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage
from mewcode_agent.prompting.models import (
    ContextBoundaryMessage,
    ContextSummaryMessage,
    ControlMessage,
    PromptFrame,
    PromptItem,
)
from mewcode_agent.providers.base import (
    LLMProvider,
    ProviderRequest,
    ProviderUsageResult,
)

ContextSummaryStartHandler = Callable[[int, int, int], None]
ContextSummaryUsageHandler = Callable[[int, ProviderUsageResult], None]


CONTEXT_BOUNDARY_TEXT = (
    "上下文压缩边界：此前部分 assistant 与 tool 细节已经由结构化摘要替代。"
    "摘要不是文件、代码、日志或工具结果的权威副本。需要精确细节时，必须使用"
    "可用读取工具重新读取摘要中给出的文件路径或 context artifact；不得根据摘要"
    "猜测、补全或声称存在未重新验证的标识符、代码、数据、错误原因或完成状态。"
)


def history_atomic_boundaries(
    messages: list[ChatMessage],
) -> tuple[int, ...]:
    """Return every valid end index without splitting a tool exchange."""

    boundaries: list[int] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "tool":
            raise ContextCompactionError(
                "context_invalid_tool_batch",
                "历史包含孤立 tool 结果",
            )
        if message.role != "assistant" or not message.tool_calls:
            index += 1
            boundaries.append(index)
            continue
        expected_ids = tuple(call.call_id for call in message.tool_calls)
        if len(expected_ids) != len(set(expected_ids)):
            raise ContextCompactionError(
                "context_invalid_tool_batch",
                "工具调用批次包含重复 call_id",
            )
        end = index + 1 + len(expected_ids)
        if end > len(messages):
            raise ContextCompactionError(
                "context_invalid_tool_batch",
                "工具调用批次缺少结果",
            )
        actual_ids: list[str] = []
        for tool_message in messages[index + 1 : end]:
            if tool_message.role != "tool" or tool_message.tool_call_id is None:
                raise ContextCompactionError(
                    "context_invalid_tool_batch",
                    "工具调用批次包含非 tool 结果",
                )
            actual_ids.append(tool_message.tool_call_id)
        if tuple(actual_ids) != expected_ids:
            raise ContextCompactionError(
                "context_invalid_tool_batch",
                "工具结果顺序或 call_id 不匹配",
            )
        index = end
        boundaries.append(index)
    return tuple(boundaries)


class ContextProjector:
    """Project one checkpoint without mutating history or control anchors."""

    def project(
        self,
        frame: PromptFrame,
        checkpoint: SummaryCheckpoint | None,
        *,
        active_request_sequence: int | None,
        active_round_number: int | None,
    ) -> PromptFrame:
        if checkpoint is None:
            return frame
        end = checkpoint.covered_history_end
        history_count = sum(
            isinstance(item, ChatMessage) for item in frame.items
        )
        if end > history_count:
            raise ContextCompactionError(
                "context_summary_invalid",
                "摘要 checkpoint 超出普通历史范围",
            )

        output: list[PromptItem] = []
        history_index = 0
        inserted = False

        def insert_checkpoint() -> None:
            nonlocal inserted
            if inserted:
                return
            output.append(
                ContextSummaryMessage(
                    checkpoint.generation,
                    checkpoint.covered_history_end,
                    checkpoint.to_json(),
                )
            )
            output.append(
                ContextBoundaryMessage(
                    checkpoint.generation,
                    CONTEXT_BOUNDARY_TEXT,
                )
            )
            inserted = True

        for item in frame.items:
            if isinstance(item, (ContextSummaryMessage, ContextBoundaryMessage)):
                raise ContextCompactionError(
                    "context_summary_invalid",
                    "完整 PromptFrame 不能预先包含压缩消息",
                )
            if isinstance(item, ControlMessage):
                if not inserted and item.anchor >= end:
                    insert_checkpoint()
                if item.anchor < end and self._expired_control(
                    item,
                    active_request_sequence=active_request_sequence,
                    active_round_number=active_round_number,
                ):
                    continue
                output.append(item)
                continue

            if not isinstance(item, ChatMessage):
                raise ContextCompactionError(
                    "context_summary_invalid",
                    "完整 PromptFrame 包含未知 item",
                )
            if not inserted and history_index >= end:
                insert_checkpoint()
            covered = history_index < end
            history_index += 1
            if covered and item.role != "user":
                continue
            output.append(item)

        if history_index != history_count:
            raise ContextCompactionError(
                "context_summary_invalid",
                "PromptFrame 普通历史计数不一致",
            )
        insert_checkpoint()
        return PromptFrame(frame.system_prompt, tuple(output))

    @staticmethod
    def _expired_control(
        message: ControlMessage,
        *,
        active_request_sequence: int | None,
        active_round_number: int | None,
    ) -> bool:
        if message.scope == "session":
            return False
        if message.scope == "request":
            return message.request_sequence != active_request_sequence
        return not (
            message.request_sequence == active_request_sequence
            and message.round_number == active_round_number
        )


@dataclass(frozen=True, slots=True)
class ContextPreparation:
    request: ProviderRequest
    estimate_before: int
    estimate_after: int
    checkpoint_changed: bool
    warning_code: str | None = None


@dataclass(frozen=True, slots=True)
class ManualCompactionResult:
    changed: bool
    generation: int
    covered_history_end: int
    estimate_before: int
    estimate_after: int


@dataclass(frozen=True, slots=True)
class RestoredHistoryPreparation:
    estimate_before: int
    estimate_after: int
    compaction: ToolCompactionResult
    summary_changed: bool


class ContextWindowManager:
    """Run preventive tool compaction before transactional summarization."""

    def __init__(
        self,
        provider: LLMProvider,
        tool_result_compactor: ToolResultCompactor,
        summarizer: ContextSummarizer,
        *,
        context_window_tokens: int,
        max_tokens: int,
        estimator: ContextTokenEstimator | None = None,
        projector: ContextProjector | None = None,
        config: CompactionConfig | None = None,
    ) -> None:
        if (
            type(context_window_tokens) is not int
            or type(max_tokens) is not int
            or max_tokens <= 0
            or context_window_tokens <= max_tokens
        ):
            raise ValueError("上下文窗口必须是大于 max_tokens 的整数")
        self._provider = provider
        self._tool_result_compactor = tool_result_compactor
        self._summarizer = summarizer
        self._config = config or CompactionConfig()
        self._estimator = estimator or ContextTokenEstimator(
            config=self._config
        )
        self._projector = projector or ContextProjector()
        self._prompt_budget_tokens = context_window_tokens - max_tokens
        self._auto_trigger_tokens = int(
            self._prompt_budget_tokens * self._config.auto_trigger_ratio
        )
        self._target_tokens = int(
            self._prompt_budget_tokens * self._config.target_ratio
        )
        self._checkpoint: SummaryCheckpoint | None = None
        self._consecutive_summary_failures = 0
        self._auto_compaction_disabled = False
        self._auto_warning_emitted = False
        self._last_auto_attempt_request_sequence: int | None = None
        self._summary_lock = asyncio.Lock()

    @property
    def checkpoint(self) -> SummaryCheckpoint | None:
        return self._checkpoint

    @property
    def consecutive_summary_failures(self) -> int:
        return self._consecutive_summary_failures

    @property
    def auto_compaction_disabled(self) -> bool:
        return self._auto_compaction_disabled

    @property
    def prompt_budget_tokens(self) -> int:
        return self._prompt_budget_tokens

    @property
    def auto_trigger_tokens(self) -> int:
        return self._auto_trigger_tokens

    @property
    def target_tokens(self) -> int:
        return self._target_tokens

    async def compact_tool_results(
        self,
        history: ConversationHistory,
    ) -> ToolCompactionResult:
        return await self._tool_result_compactor.compact(history)

    async def prepare_agent_request(
        self,
        history: ConversationHistory,
        *,
        compose_frame: Callable[[], PromptFrame],
        tools: tuple[dict[str, Any], ...] | None,
        active_request_sequence: int | None,
        active_round_number: int | None,
        on_summary_start: ContextSummaryStartHandler | None = None,
        on_summary_usage: ContextSummaryUsageHandler | None = None,
    ) -> ContextPreparation:
        await self.compact_tool_results(history)
        frame = compose_frame()
        projected = self._projector.project(
            frame,
            self._checkpoint,
            active_request_sequence=active_request_sequence,
            active_round_number=active_round_number,
        )
        request = ProviderRequest(
            projected.system_prompt,
            projected.items,
            tools,
        )
        before = self._estimator.estimate(self._provider, request)
        if before.estimated_prompt_tokens < self._auto_trigger_tokens:
            return ContextPreparation(
                request,
                before.estimated_prompt_tokens,
                before.estimated_prompt_tokens,
                False,
            )
        if self._auto_compaction_disabled:
            if before.estimated_prompt_tokens >= self._prompt_budget_tokens:
                raise ContextCompactionError(
                    "context_window_exceeded",
                    "上下文超过模型 Prompt 预算且自动压缩已熔断",
                )
            warning = None
            if not self._auto_warning_emitted:
                warning = "context_auto_compaction_disabled"
                self._auto_warning_emitted = True
            return ContextPreparation(
                request,
                before.estimated_prompt_tokens,
                before.estimated_prompt_tokens,
                False,
                warning,
            )
        if (
            active_request_sequence is not None
            and self._last_auto_attempt_request_sequence
            == active_request_sequence
        ):
            if before.estimated_prompt_tokens >= self._prompt_budget_tokens:
                raise ContextCompactionError(
                    "context_window_exceeded",
                    "上下文超过模型 Prompt 预算且本请求已尝试自动压缩",
                )
            return ContextPreparation(
                request,
                before.estimated_prompt_tokens,
                before.estimated_prompt_tokens,
                False,
            )

        async with self._summary_lock:
            if active_request_sequence is not None:
                self._last_auto_attempt_request_sequence = (
                    active_request_sequence
                )
            try:
                boundary = self._automatic_boundary(
                    history,
                    frame,
                    tools=tools,
                    active_request_sequence=active_request_sequence,
                    active_round_number=active_round_number,
                )
                if on_summary_start is not None:
                    generation = (
                        self._checkpoint.generation + 1
                        if self._checkpoint is not None
                        else 1
                    )
                    on_summary_start(
                        generation,
                        boundary,
                        before.estimated_prompt_tokens,
                    )
                preparation = await self._summarize_to_boundary(
                    history,
                    frame,
                    boundary=boundary,
                    tools=tools,
                    active_request_sequence=active_request_sequence,
                    active_round_number=active_round_number,
                    estimate_before=before,
                    require_target=True,
                    on_summary_usage=on_summary_usage,
                )
            except ContextCompactionError as exc:
                self._register_failure()
                if before.estimated_prompt_tokens >= self._prompt_budget_tokens:
                    raise ContextCompactionError(
                        "context_window_exceeded",
                        "上下文超过模型 Prompt 预算且压缩失败",
                    ) from exc
                warning = (
                    "context_auto_compaction_disabled"
                    if self._auto_compaction_disabled
                    else exc.code
                )
                if self._auto_compaction_disabled:
                    if self._auto_warning_emitted:
                        warning = None
                    else:
                        self._auto_warning_emitted = True
                return ContextPreparation(
                    request,
                    before.estimated_prompt_tokens,
                    before.estimated_prompt_tokens,
                    False,
                    warning,
                )
        return preparation

    async def compact_now(
        self,
        history: ConversationHistory,
        *,
        compose_frame: Callable[[], PromptFrame],
        tools: tuple[dict[str, Any], ...] | None,
        on_summary_usage: ContextSummaryUsageHandler | None = None,
    ) -> ManualCompactionResult:
        await self.compact_tool_results(history)
        frame = compose_frame()
        projected = self._projector.project(
            frame,
            self._checkpoint,
            active_request_sequence=None,
            active_round_number=None,
        )
        request = ProviderRequest(
            projected.system_prompt,
            projected.items,
            tools,
        )
        before = self._estimator.estimate(self._provider, request)
        boundaries = self._new_boundaries(history.snapshot())
        if not boundaries:
            checkpoint = self._checkpoint
            return ManualCompactionResult(
                False,
                checkpoint.generation if checkpoint is not None else 0,
                (
                    checkpoint.covered_history_end
                    if checkpoint is not None
                    else 0
                ),
                before.estimated_prompt_tokens,
                before.estimated_prompt_tokens,
            )
        if before.estimated_prompt_tokens >= self._auto_trigger_tokens:
            boundary = self._automatic_boundary(
                history,
                frame,
                tools=tools,
                active_request_sequence=None,
                active_round_number=None,
            )
            require_target = True
        else:
            all_boundaries = history_atomic_boundaries(history.snapshot())
            retain = self._config.manual_retained_units
            if len(all_boundaries) <= retain:
                checkpoint = self._checkpoint
                return ManualCompactionResult(
                    False,
                    checkpoint.generation if checkpoint is not None else 0,
                    (
                        checkpoint.covered_history_end
                        if checkpoint is not None
                        else 0
                    ),
                    before.estimated_prompt_tokens,
                    before.estimated_prompt_tokens,
                )
            boundary = all_boundaries[len(all_boundaries) - retain - 1]
            if self._checkpoint is not None and boundary <= (
                self._checkpoint.covered_history_end
            ):
                return ManualCompactionResult(
                    False,
                    self._checkpoint.generation,
                    self._checkpoint.covered_history_end,
                    before.estimated_prompt_tokens,
                    before.estimated_prompt_tokens,
                )
            require_target = False

        async with self._summary_lock:
            try:
                prepared = await self._summarize_to_boundary(
                    history,
                    frame,
                    boundary=boundary,
                    tools=tools,
                    active_request_sequence=None,
                    active_round_number=None,
                    estimate_before=before,
                    require_target=require_target,
                    on_summary_usage=on_summary_usage,
                )
            except ContextCompactionError:
                self._register_failure()
                raise
        checkpoint = self._checkpoint
        assert checkpoint is not None
        return ManualCompactionResult(
            True,
            checkpoint.generation,
            checkpoint.covered_history_end,
            prepared.estimate_before,
            prepared.estimate_after,
        )

    def record_usage(
        self,
        request: ProviderRequest,
        result: ProviderUsageResult,
    ) -> None:
        self._estimator.record_usage(self._provider, request, result)

    def inspect_status(
        self,
        frame: PromptFrame,
        *,
        tools: tuple[dict[str, Any], ...] | None,
    ) -> ContextStatus:
        projected = self._projector.project(
            frame,
            self._checkpoint,
            active_request_sequence=None,
            active_round_number=None,
        )
        estimate = self._estimator.estimate(
            self._provider,
            ProviderRequest(projected.system_prompt, projected.items, tools),
        )
        checkpoint = self._checkpoint
        return ContextStatus(
            estimate.estimated_prompt_tokens,
            estimate.used_actual_baseline,
            self._prompt_budget_tokens,
            self._auto_trigger_tokens,
            checkpoint.generation if checkpoint is not None else 0,
            checkpoint.covered_history_end if checkpoint is not None else 0,
            self._consecutive_summary_failures,
            self._auto_compaction_disabled,
        )

    def reset_session(self) -> None:
        if self._summary_lock.locked():
            raise RuntimeError("上下文摘要运行期间不能重置 session")
        self._tool_result_compactor.reset_session()
        self._estimator.reset_session()
        self._checkpoint = None
        self._consecutive_summary_failures = 0
        self._auto_compaction_disabled = False
        self._auto_warning_emitted = False
        self._last_auto_attempt_request_sequence = None

    async def prepare_restored_history(
        self,
        history: ConversationHistory,
        *,
        compose_frame: Callable[[], PromptFrame],
        tools: tuple[dict[str, Any], ...] | None,
        on_summary_usage: ContextSummaryUsageHandler | None = None,
    ) -> RestoredHistoryPreparation:
        compaction = await self.compact_tool_results(history)
        frame = compose_frame()
        request = ProviderRequest(frame.system_prompt, frame.items, tools)
        before = self._estimator.estimate(self._provider, request)
        if before.estimated_prompt_tokens < self._prompt_budget_tokens:
            return RestoredHistoryPreparation(
                before.estimated_prompt_tokens,
                before.estimated_prompt_tokens,
                compaction,
                False,
            )
        result = await self.compact_now(
            history,
            compose_frame=compose_frame,
            tools=tools,
            on_summary_usage=on_summary_usage,
        )
        if (
            not result.changed
            or result.estimate_after >= self._prompt_budget_tokens
        ):
            raise ContextCompactionError(
                "context_window_exceeded",
                "恢复历史超过模型 Prompt 预算且无法压缩",
            )
        return RestoredHistoryPreparation(
            result.estimate_before,
            result.estimate_after,
            compaction,
            True,
        )

    def _new_boundaries(
        self,
        messages: list[ChatMessage],
    ) -> tuple[int, ...]:
        current_end = (
            self._checkpoint.covered_history_end
            if self._checkpoint is not None
            else 0
        )
        return tuple(
            boundary
            for boundary in history_atomic_boundaries(messages)
            if boundary > current_end
        )

    def _automatic_boundary(
        self,
        history: ConversationHistory,
        frame: PromptFrame,
        *,
        tools: tuple[dict[str, Any], ...] | None,
        active_request_sequence: int | None,
        active_round_number: int | None,
    ) -> int:
        messages = history.snapshot()
        boundaries = self._new_boundaries(messages)
        if not boundaries:
            raise ContextCompactionError(
                "context_history_not_compressible",
                "没有新的完整历史单元可供压缩",
            )
        generation = (
            self._checkpoint.generation + 1
            if self._checkpoint is not None
            else 1
        )
        placeholder = "x" * self._config.summary_response_bytes
        for boundary in boundaries:
            checkpoint = SummaryCheckpoint(
                generation,
                boundary,
                SummarySections(
                    primary_requests=(),
                    key_concepts=(),
                    files_and_code=(),
                    errors_and_fixes=(),
                    solution_process=(placeholder,),
                    pending_tasks=(),
                    current_work=(),
                    next_step=(),
                ),
                self._verbatim_users(messages, boundary),
            )
            projected = self._projector.project(
                frame,
                checkpoint,
                active_request_sequence=active_request_sequence,
                active_round_number=active_round_number,
            )
            estimate = self._estimator.estimate(
                self._provider,
                ProviderRequest(projected.system_prompt, projected.items, tools),
            )
            if estimate.estimated_prompt_tokens <= self._target_tokens:
                return boundary
        raise ContextCompactionError(
            "context_history_not_compressible",
            "不可压缩内容本身超过摘要目标预算",
        )

    async def _summarize_to_boundary(
        self,
        history: ConversationHistory,
        frame: PromptFrame,
        *,
        boundary: int,
        tools: tuple[dict[str, Any], ...] | None,
        active_request_sequence: int | None,
        active_round_number: int | None,
        estimate_before: ContextEstimate,
        require_target: bool,
        on_summary_usage: ContextSummaryUsageHandler | None,
    ) -> ContextPreparation:
        messages = history.snapshot()
        previous = self._checkpoint
        history_start = previous.covered_history_end if previous is not None else 0
        if boundary <= history_start or boundary > len(messages):
            raise ContextCompactionError(
                "context_summary_invalid",
                "摘要候选边界无效",
            )
        generated = await self._summarizer.summarize(
            previous=previous,
            history_start=history_start,
            history_end=boundary,
            messages=tuple(messages[history_start:boundary]),
            on_usage=(
                (
                    lambda result: on_summary_usage(
                        previous.generation + 1
                        if previous is not None
                        else 1,
                        result,
                    )
                )
                if on_summary_usage is not None
                else None
            ),
        )
        checkpoint = SummaryCheckpoint(
            previous.generation + 1 if previous is not None else 1,
            boundary,
            generated.sections,
            self._verbatim_users(messages, boundary),
        )
        projected = self._projector.project(
            frame,
            checkpoint,
            active_request_sequence=active_request_sequence,
            active_round_number=active_round_number,
        )
        request = ProviderRequest(projected.system_prompt, projected.items, tools)
        after = self._estimator.estimate(self._provider, request)
        if after.estimated_prompt_tokens >= estimate_before.estimated_prompt_tokens:
            raise ContextCompactionError(
                "context_summary_insufficient_reduction",
                "上下文摘要没有降低 Prompt 估值",
            )
        if require_target and after.estimated_prompt_tokens > self._target_tokens:
            raise ContextCompactionError(
                "context_summary_insufficient_reduction",
                "上下文摘要未达到目标预算",
            )
        self._checkpoint = checkpoint
        self._reset_failures()
        return ContextPreparation(
            request,
            estimate_before.estimated_prompt_tokens,
            after.estimated_prompt_tokens,
            True,
        )

    @staticmethod
    def _verbatim_users(
        messages: list[ChatMessage],
        boundary: int,
    ) -> tuple[VerbatimUserMessage, ...]:
        return tuple(
            VerbatimUserMessage(index, message.content)
            for index, message in enumerate(messages[:boundary])
            if message.role == "user"
        )

    def _register_failure(self) -> None:
        self._consecutive_summary_failures += 1
        if self._consecutive_summary_failures >= (
            self._config.max_summary_failures
        ):
            self._auto_compaction_disabled = True

    def _reset_failures(self) -> None:
        self._consecutive_summary_failures = 0
        self._auto_compaction_disabled = False
        self._auto_warning_emitted = False
