"""UI-independent ReAct loop with structured events and plan approvals."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from mewcode_agent.agent.context import AgentRunCancelled, AgentRunContext
from mewcode_agent.agent.events import (
    AgentEvent,
    AgentRunMode,
    AgentRunState,
    ContextCompactionCompletedEvent,
    ContextCompactionStartedEvent,
    ContextCompactionWarningEvent,
    FinalResponseEvent,
    ModelTextEvent,
    ModelThinkingEvent,
    PlanApprovalRequestedEvent,
    RoundStartedEvent,
    RunCancelledEvent,
    RunErrorEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from mewcode_agent.agent.tool_scheduler import ToolScheduler
from mewcode_agent.agent.usage import (
    CompactionUsageRecord,
    UsageCollector,
    UsageRecord,
)
from mewcode_agent.compaction import (
    ContextCompactionError,
    ContextPreparation,
    ContextStatus,
    ContextWindowManager,
    ManualCompactionResult,
    RestoredHistoryPreparation,
)
from mewcode_agent.history import ConversationHistory
from mewcode_agent.hooks import HookEngine, HookEventName
from mewcode_agent.models import ThinkingBlock, ToolCall
from mewcode_agent.prompting.builtins import PLAN_APPROVED_TEXT
from mewcode_agent.prompting.composer import PromptComposer
from mewcode_agent.prompting.models import RuntimeInstruction
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.providers.base import (
    LLMProvider,
    ProviderError,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderThinkingComplete,
    ProviderThinkingDelta,
    ProviderToolCall,
    ProviderTurnEnd,
    ProviderUsageEvent,
    ProviderUsageResult,
)
from mewcode_agent.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    max_rounds: int = 15
    llm_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.max_rounds <= 0:
            raise ValueError("max_rounds 必须大于 0")
        if self.llm_timeout_seconds <= 0:
            raise ValueError("llm_timeout_seconds 必须大于 0")


@dataclass(slots=True)
class _RoundData:
    text_parts: list[str] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    saw_thinking: bool = False
    usage_result: ProviderUsageResult | None = None
    turn_end: ProviderTurnEnd | None = None


@dataclass(frozen=True, slots=True)
class _ProviderFailure:
    error: Exception


@dataclass(frozen=True, slots=True)
class _ContextPreparationComplete:
    preparation: ContextPreparation


class _AgentLoopFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_PROVIDER_DONE = object()

RequestControlProvider = Callable[
    [], Awaitable[tuple[RuntimeInstruction, ...]]
]


class AgentLoop:
    """Run one user request through LLM/tool turns."""

    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        *,
        prompt_runtime: PromptRuntime,
        prompt_composer: PromptComposer,
        config: AgentLoopConfig | None = None,
        scheduler: ToolScheduler | None = None,
        usage_collector: UsageCollector | None = None,
        context_window_manager: ContextWindowManager | None,
        visible_tool_names: Callable[[], frozenset[str] | None] | None = None,
        hook_engine: HookEngine | None = None,
        request_control_provider: RequestControlProvider | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._prompt_runtime = prompt_runtime
        self._prompt_composer = prompt_composer
        self._config = config or AgentLoopConfig()
        self._scheduler = scheduler or ToolScheduler(registry)
        self._usage_collector = usage_collector
        self._context_window_manager = context_window_manager
        self._visible_tool_names_provider = visible_tool_names
        self._hook_engine = hook_engine
        self._request_control_provider = request_control_provider

    async def _dispatch_hook(
        self,
        event: HookEventName,
        values: dict[str, Any] | None = None,
    ) -> None:
        if self._hook_engine is None:
            return
        await self._hook_engine.dispatch(event, values)

    async def _run_error_event(
        self,
        code: str,
        message: str,
    ) -> RunErrorEvent:
        await self._dispatch_hook(
            "system.error",
            {"error.code": code, "error.message": message},
        )
        return RunErrorEvent(code, message)

    def _visible_tool_names(self) -> frozenset[str] | None:
        if self._visible_tool_names_provider is None:
            return None
        value = self._visible_tool_names_provider()
        if value is not None and not isinstance(value, frozenset):
            raise ValueError("visible_tool_names provider 返回值无效")
        return value

    async def compact_history(
        self,
        history: ConversationHistory,
    ) -> ManualCompactionResult:
        """Run one manual compaction without creating a Prompt request."""

        manager = self._context_window_manager
        if manager is None:
            raise ContextCompactionError(
                "context_summary_failed",
                "当前 Agent 未配置上下文压缩",
            )
        try:
            tools = tuple(
                self._registry.api_tools(
                    self._provider.protocol,
                    visible_names=self._visible_tool_names(),
                )
            )

            def record_usage(
                generation: int,
                result: ProviderUsageResult,
            ) -> None:
                if self._usage_collector is not None:
                    self._usage_collector.record(
                        CompactionUsageRecord(
                            self._provider.provider_id,
                            generation,
                            result,
                        )
                    )

            summary_attempt: tuple[int, int, int] | None = None

            async def on_summary_start(
                generation: int,
                covered_messages: int,
                estimate_before: int,
            ) -> None:
                nonlocal summary_attempt
                summary_attempt = (
                    generation,
                    covered_messages,
                    estimate_before,
                )
                await self._dispatch_hook(
                    "context.before_compaction",
                    {
                        "compaction.generation": generation,
                        "compaction.covered_messages": covered_messages,
                        "compaction.estimate_before": estimate_before,
                    },
                )

            try:
                result = await manager.compact_now(
                    history,
                    compose_frame=lambda: self._prompt_composer.compose(
                        history.snapshot(),
                        self._prompt_runtime.timeline(),
                    ),
                    tools=tools,
                    on_summary_start=on_summary_start,
                    on_summary_usage=record_usage,
                )
            except ContextCompactionError as exc:
                if summary_attempt is not None:
                    generation, covered, estimate_before = summary_attempt
                    await self._dispatch_hook(
                        "context.after_compaction",
                        {
                            "compaction.generation": generation,
                            "compaction.covered_messages": covered,
                            "compaction.estimate_before": estimate_before,
                            "compaction.estimate_after": estimate_before,
                            "compaction.success": False,
                            "compaction.error_code": exc.code,
                        },
                    )
                raise
            if summary_attempt is not None:
                await self._dispatch_hook(
                    "context.after_compaction",
                    {
                        "compaction.generation": result.generation,
                        "compaction.covered_messages": (
                            result.covered_history_end
                        ),
                        "compaction.estimate_before": result.estimate_before,
                        "compaction.estimate_after": result.estimate_after,
                        "compaction.success": result.changed,
                    },
                )
            return result
        except ContextCompactionError:
            raise
        except (ValueError, RuntimeError) as exc:
            raise ContextCompactionError(
                "context_summary_failed",
                "无法生成上下文压缩请求",
            ) from exc

    def context_status(
        self,
        history: ConversationHistory,
    ) -> ContextStatus | None:
        manager = self._context_window_manager
        if manager is None:
            return None
        tools = tuple(
            self._registry.api_tools(
                self._provider.protocol,
                visible_names=self._visible_tool_names(),
            )
        )
        frame = self._prompt_composer.compose(
            history.snapshot(),
            self._prompt_runtime.timeline(),
        )
        return manager.inspect_status(frame, tools=tools)

    def reset_session(
        self,
        *,
        session_controls: tuple[RuntimeInstruction, ...],
    ) -> None:
        if self._context_window_manager is not None:
            self._context_window_manager.reset_session()
        self._prompt_runtime.reset_session(
            session_controls=session_controls,
        )

    async def prepare_restored_history(
        self,
        history: ConversationHistory,
    ) -> RestoredHistoryPreparation | None:
        manager = self._context_window_manager
        if manager is None:
            return None
        tools = tuple(
            self._registry.api_tools(
                self._provider.protocol,
                visible_names=self._visible_tool_names(),
            )
        )

        def record_usage(
            generation: int,
            result: ProviderUsageResult,
        ) -> None:
            if self._usage_collector is not None:
                self._usage_collector.record(
                    CompactionUsageRecord(
                        self._provider.provider_id,
                        generation,
                        result,
                    )
                )

        return await manager.prepare_restored_history(
            history,
            compose_frame=lambda: self._prompt_composer.compose(
                history.snapshot(),
                self._prompt_runtime.timeline(),
            ),
            tools=tools,
            on_summary_usage=record_usage,
        )

    async def run(
        self,
        user_message: str,
        history: ConversationHistory,
        *,
        plan_only: bool,
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        if not isinstance(user_message, str) or not user_message.strip():
            raise ValueError("user_message 必须为非空字符串")

        context.begin_run()
        initial_mode: AgentRunMode = (
            "planning" if plan_only else "executing"
        )
        state: AgentRunState = initial_mode
        current_request_authorized = False
        round_number = 0
        request_started = False

        try:
            try:
                request_sequence = await self._prompt_runtime.begin_request(
                    history_length=len(history.snapshot()),
                    mode=initial_mode,
                )
                if self._request_control_provider is not None:
                    controls = await self._request_control_provider()
                    if not isinstance(controls, tuple):
                        raise ValueError(
                            "request_control_provider 必须返回 tuple"
                        )
                    for control in controls:
                        if (
                            not isinstance(control, RuntimeInstruction)
                            or control.scope != "request"
                        ):
                            raise ValueError(
                                "request control 必须是 scope=request"
                            )
                        self._prompt_runtime.inject(
                            control,
                            history_length=len(history.snapshot()),
                        )
            except (ValueError, RuntimeError):
                state = "failed"
                yield await self._run_error_event(
                    "prompt_error", "无法生成本轮模型请求"
                )
                return
            request_started = True
            if self._hook_engine is not None:
                await self._hook_engine.flush_pending_prompts()
            history.add_user(user_message)
            await self._dispatch_hook(
                "message.before_send",
                {
                    "message.content": user_message,
                    "message.kind": "user",
                },
            )
            yield UserMessageEvent(user_message)

            while round_number < self._config.max_rounds:
                if context.cancelled:
                    state = "cancelled"
                    yield RunCancelledEvent("user_cancelled")
                    return

                round_number += 1
                mode: AgentRunMode = (
                    "planning" if state == "planning" else "executing"
                )
                yield RoundStartedEvent(
                    round_number,
                    self._config.max_rounds,
                    mode,
                )

                is_final_round = round_number == self._config.max_rounds
                round_started = False
                round_outcome = "failed"
                try:
                    try:
                        self._prompt_runtime.begin_round(
                            history_length=len(history.snapshot()),
                            round_number=round_number,
                            max_rounds=self._config.max_rounds,
                            mode=mode,
                        )
                        round_started = True
                        await self._dispatch_hook(
                            "round.started",
                            {
                                "round.number": round_number,
                                "round.max_rounds": self._config.max_rounds,
                                "round.mode": mode,
                            },
                        )
                        self._prompt_runtime.seal_round()
                        visible_names = self._visible_tool_names()
                        api_tools = (
                            None
                            if is_final_round
                            else self._registry.api_tools(
                                self._provider.protocol,
                                visible_names=visible_names,
                            )
                        )
                        tools = (
                            tuple(api_tools)
                            if api_tools is not None
                            else None
                        )
                        manager = self._context_window_manager
                        if manager is None:
                            frame = self._prompt_composer.compose(
                                history.snapshot(),
                                self._prompt_runtime.timeline(),
                            )
                            provider_request = ProviderRequest(
                                frame.system_prompt,
                                frame.items,
                                tools,
                            )
                        else:
                            provider_request = None
                            async for context_event in (
                                self._prepare_context_request(
                                    manager,
                                    history,
                                    tools=tools,
                                    context=context,
                                )
                            ):
                                if isinstance(
                                    context_event,
                                    _ContextPreparationComplete,
                                ):
                                    provider_request = (
                                        context_event.preparation.request
                                    )
                                else:
                                    yield context_event
                            assert provider_request is not None
                    except AgentRunCancelled:
                        state = "cancelled"
                        round_outcome = "cancelled"
                        yield RunCancelledEvent("user_cancelled")
                        return
                    except ContextCompactionError as exc:
                        state = "failed"
                        yield await self._run_error_event(exc.code, exc.message)
                        return
                    except (ValueError, RuntimeError):
                        state = "failed"
                        yield await self._run_error_event(
                            "prompt_error", "无法生成本轮模型请求"
                        )
                        return

                    round_data = _RoundData()
                    try:
                        provider_stream = self._provider.stream_chat(
                            provider_request
                        )
                        async for event in self._consume_provider_round(
                            provider_stream,
                            context=context,
                            round_data=round_data,
                        ):
                            yield event
                    except AgentRunCancelled:
                        state = "cancelled"
                        round_outcome = "cancelled"
                        yield RunCancelledEvent("user_cancelled")
                        return
                    except TimeoutError:
                        state = "failed"
                        yield await self._run_error_event(
                            "llm_timeout",
                            (
                                "模型单轮调用超过 "
                                f"{self._config.llm_timeout_seconds:g} 秒"
                            ),
                        )
                        return
                    except ProviderError as exc:
                        state = "failed"
                        yield await self._run_error_event(
                            "provider_error", str(exc)
                        )
                        return
                    except _AgentLoopFailure as exc:
                        state = "failed"
                        yield await self._run_error_event(exc.code, exc.message)
                        return
                    except Exception:
                        state = "failed"
                        yield await self._run_error_event(
                            "provider_error", "模型调用失败"
                        )
                        return

                    if self._usage_collector is not None:
                        assert round_data.usage_result is not None
                        self._usage_collector.record(
                            UsageRecord(
                                self._provider.provider_id,
                                request_sequence,
                                round_number,
                                mode,
                                round_data.usage_result,
                            )
                        )

                    if self._context_window_manager is not None:
                        assert round_data.usage_result is not None
                        self._context_window_manager.record_usage(
                            provider_request,
                            round_data.usage_result,
                        )

                    turn_end = round_data.turn_end
                    if turn_end is None:
                        state = "failed"
                        yield await self._run_error_event(
                            "invalid_provider_stream",
                            "Provider 流缺少结束事件",
                        )
                        return

                    has_tool_calls = bool(round_data.tool_calls)
                    if is_final_round and has_tool_calls:
                        state = "failed"
                        yield await self._run_error_event(
                            "max_rounds_exceeded",
                            "最终模型轮仍返回了工具调用",
                        )
                        return

                    if (
                        has_tool_calls
                        and round_data.saw_thinking
                        and not round_data.thinking_blocks
                    ):
                        state = "failed"
                        yield await self._run_error_event(
                            "invalid_provider_stream",
                            "工具调用轮缺少完整 thinking 元数据",
                        )
                        return

                    if turn_end.stop_reason == "max_tokens":
                        state = "failed"
                        yield await self._run_error_event(
                            "max_tokens_reached",
                            "模型达到 Token 上限，未能完成当前响应",
                        )
                        return

                    if has_tool_calls != (
                        turn_end.stop_reason == "tool_calls"
                    ):
                        state = "failed"
                        yield await self._run_error_event(
                            "invalid_provider_stream",
                            "Provider 停止原因与工具调用不一致",
                        )
                        return

                    if has_tool_calls:
                        await self._dispatch_hook(
                            "message.after_receive",
                            {
                                "message.content": "".join(
                                    round_data.text_parts
                                ),
                                "message.kind": "assistant",
                            },
                        )
                        history.add_assistant_tool_calls(
                            "".join(round_data.text_parts),
                            tuple(round_data.tool_calls),
                            thinking_blocks=tuple(
                                round_data.thinking_blocks
                            ),
                        )
                        async for event in self._scheduler.run(
                            tuple(round_data.tool_calls),
                            plan_only=plan_only,
                            current_request_authorized=(
                                current_request_authorized
                            ),
                            context=context,
                            visible_names=visible_names,
                        ):
                            if isinstance(event, ToolResultEvent):
                                history.add_tool_result(
                                    event.call_id,
                                    event.result,
                                )
                            yield event
                        if context.cancelled:
                            state = "cancelled"
                            round_outcome = "cancelled"
                            yield RunCancelledEvent("user_cancelled")
                            return
                        round_outcome = "continued"
                        continue

                    content = "".join(round_data.text_parts)
                    if not content.strip():
                        state = "failed"
                        if round_data.saw_thinking:
                            yield await self._run_error_event(
                                "invalid_provider_stream",
                                "Provider 只返回了 thinking，没有正文",
                            )
                        else:
                            yield await self._run_error_event(
                                "empty_response",
                                "模型没有返回正文、thinking 或工具调用",
                            )
                        return

                    await self._dispatch_hook(
                        "message.after_receive",
                        {
                            "message.content": content,
                            "message.kind": "assistant",
                        },
                    )

                    if mode == "executing":
                        history.add_assistant(content)
                        state = "completed"
                        round_outcome = "completed"
                        yield FinalResponseEvent(content, round_number)
                        return

                    history.add_assistant(content)
                    state = "waiting_plan_approval"
                    request_id = context.open_plan_approval()
                    yield PlanApprovalRequestedEvent(
                        request_id=request_id,
                        plan=content,
                        can_execute=not is_final_round,
                        can_request_changes=not is_final_round,
                    )
                    try:
                        resolution = await context.wait_for_plan_approval(
                            request_id
                        )
                    except AgentRunCancelled:
                        state = "cancelled"
                        round_outcome = "cancelled"
                        yield RunCancelledEvent("user_cancelled")
                        return

                    if is_final_round:
                        state = "cancelled"
                        round_outcome = "cancelled"
                        yield RunCancelledEvent("round_limit_after_plan")
                        return
                    if resolution.decision == "execute_current":
                        self._prompt_runtime.inject(
                            RuntimeInstruction(
                                (
                                    "runtime.plan.approved."
                                    f"request_{request_sequence}"
                                ),
                                "instruction",
                                "request",
                                PLAN_APPROVED_TEXT,
                                "plan_approval",
                            ),
                            history_length=len(history.snapshot()),
                        )
                        current_request_authorized = True
                        state = "executing"
                        round_outcome = "continued"
                    elif resolution.decision == "request_changes":
                        history.add_user(resolution.feedback)
                        await self._dispatch_hook(
                            "message.before_send",
                            {
                                "message.content": resolution.feedback,
                                "message.kind": "user",
                            },
                        )
                        state = "planning"
                        round_outcome = "continued"
                        yield UserMessageEvent(resolution.feedback)
                    else:
                        state = "cancelled"
                        round_outcome = "cancelled"
                        yield RunCancelledEvent("plan_rejected")
                        return
                finally:
                    if round_started:
                        try:
                            await self._dispatch_hook(
                                "round.ended",
                                {
                                    "round.number": round_number,
                                    "round.max_rounds": (
                                        self._config.max_rounds
                                    ),
                                    "round.mode": mode,
                                    "round.outcome": round_outcome,
                                },
                            )
                        finally:
                            self._prompt_runtime.end_round()

            state = "failed"
            yield await self._run_error_event(
                "max_rounds_exceeded",
                "当前请求已达到模型轮数上限",
            )
        finally:
            if request_started:
                self._prompt_runtime.end_request()
            context.finish_run()

    async def _prepare_context_request(
        self,
        manager: ContextWindowManager,
        history: ConversationHistory,
        *,
        tools: tuple[dict[str, Any], ...] | None,
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent | _ContextPreparationComplete]:
        started_events: asyncio.Queue[ContextCompactionStartedEvent] = (
            asyncio.Queue()
        )
        summary_attempt: tuple[int, int, int] | None = None

        async def on_summary_start(
            generation: int,
            covered_messages: int,
            estimate_before: int,
        ) -> None:
            nonlocal summary_attempt
            summary_attempt = (
                generation,
                covered_messages,
                estimate_before,
            )
            started_events.put_nowait(
                ContextCompactionStartedEvent(
                    generation,
                    covered_messages,
                    estimate_before,
                )
            )
            await self._dispatch_hook(
                "context.before_compaction",
                {
                    "compaction.generation": generation,
                    "compaction.covered_messages": covered_messages,
                    "compaction.estimate_before": estimate_before,
                },
            )

        def on_summary_usage(
            generation: int,
            result: ProviderUsageResult,
        ) -> None:
            if self._usage_collector is not None:
                self._usage_collector.record(
                    CompactionUsageRecord(
                        self._provider.provider_id,
                        generation,
                        result,
                    )
                )

        preparation_task = asyncio.create_task(
            manager.prepare_agent_request(
                history,
                compose_frame=lambda: self._prompt_composer.compose(
                    history.snapshot(),
                    self._prompt_runtime.timeline(),
                ),
                tools=tools,
                active_request_sequence=(
                    self._prompt_runtime.active_request_sequence
                ),
                active_round_number=(
                    self._prompt_runtime.active_round_number
                ),
                on_summary_start=on_summary_start,
                on_summary_usage=on_summary_usage,
            )
        )
        cancel_task = asyncio.create_task(context.wait_cancelled())
        try:
            while True:
                started_task = asyncio.create_task(started_events.get())
                done, _ = await asyncio.wait(
                    {preparation_task, cancel_task, started_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    started_task.cancel()
                    preparation_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await started_task
                    with suppress(asyncio.CancelledError):
                        await preparation_task
                    if summary_attempt is not None:
                        generation, covered, estimate_before = summary_attempt
                        await self._dispatch_hook(
                            "context.after_compaction",
                            {
                                "compaction.generation": generation,
                                "compaction.covered_messages": covered,
                                "compaction.estimate_before": estimate_before,
                                "compaction.estimate_after": estimate_before,
                                "compaction.success": False,
                                "compaction.error_code": (
                                    "context_compaction_cancelled"
                                ),
                            },
                        )
                    raise AgentRunCancelled
                if started_task in done:
                    yield started_task.result()
                else:
                    started_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await started_task
                if preparation_task not in done:
                    continue

                try:
                    preparation = preparation_task.result()
                except ContextCompactionError as exc:
                    if summary_attempt is not None:
                        generation, covered, estimate_before = summary_attempt
                        await self._dispatch_hook(
                            "context.after_compaction",
                            {
                                "compaction.generation": generation,
                                "compaction.covered_messages": covered,
                                "compaction.estimate_before": estimate_before,
                                "compaction.estimate_after": estimate_before,
                                "compaction.success": False,
                                "compaction.error_code": exc.code,
                            },
                        )
                    raise
                while not started_events.empty():
                    yield started_events.get_nowait()
                checkpoint = manager.checkpoint
                if summary_attempt is not None:
                    generation, covered, _estimate_before = summary_attempt
                    await self._dispatch_hook(
                        "context.after_compaction",
                        {
                            "compaction.generation": (
                                checkpoint.generation
                                if preparation.checkpoint_changed
                                and checkpoint is not None
                                else generation
                            ),
                            "compaction.covered_messages": (
                                checkpoint.covered_history_end
                                if preparation.checkpoint_changed
                                and checkpoint is not None
                                else covered
                            ),
                            "compaction.estimate_before": (
                                preparation.estimate_before
                            ),
                            "compaction.estimate_after": (
                                preparation.estimate_after
                            ),
                            "compaction.success": (
                                preparation.checkpoint_changed
                            ),
                            **(
                                {
                                    "compaction.error_code": (
                                        preparation.warning_code
                                    )
                                }
                                if preparation.warning_code is not None
                                else {}
                            ),
                        },
                    )
                if preparation.checkpoint_changed:
                    assert checkpoint is not None
                    yield ContextCompactionCompletedEvent(
                        checkpoint.generation,
                        checkpoint.covered_history_end,
                        preparation.estimate_before,
                        preparation.estimate_after,
                    )
                if preparation.warning_code is not None:
                    warning_generation = (
                        summary_attempt[0]
                        if summary_attempt is not None
                        else (
                            checkpoint.generation
                            if checkpoint is not None
                            else 0
                        )
                    )
                    warning_coverage = (
                        summary_attempt[1]
                        if summary_attempt is not None
                        else (
                            checkpoint.covered_history_end
                            if checkpoint is not None
                            else 0
                        )
                    )
                    yield ContextCompactionWarningEvent(
                        preparation.warning_code,
                        warning_generation,
                        warning_coverage,
                        preparation.estimate_before,
                        preparation.estimate_after,
                    )
                yield _ContextPreparationComplete(preparation)
                return
        finally:
            cancel_task.cancel()
            if not preparation_task.done():
                preparation_task.cancel()
            with suppress(asyncio.CancelledError):
                await preparation_task

    async def _consume_provider_round(
        self,
        stream: AsyncIterator[ProviderStreamEvent],
        *,
        context: AgentRunContext,
        round_data: _RoundData,
    ) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue[object] = asyncio.Queue()

        async def produce() -> None:
            try:
                async with asyncio.timeout(
                    self._config.llm_timeout_seconds
                ):
                    async for event in stream:
                        queue.put_nowait(event)
            except Exception as exc:
                queue.put_nowait(_ProviderFailure(exc))
            finally:
                queue.put_nowait(_PROVIDER_DONE)

        producer_task = asyncio.create_task(produce())
        cancel_task = asyncio.create_task(context.wait_cancelled())
        try:
            while True:
                item_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {item_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    item_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await item_task
                    producer_task.cancel()
                    raise AgentRunCancelled

                item = item_task.result()
                if item is _PROVIDER_DONE:
                    break
                if isinstance(item, _ProviderFailure):
                    raise item.error
                if round_data.turn_end is not None:
                    raise _AgentLoopFailure(
                        "invalid_provider_stream",
                        "Provider 结束事件之后仍返回了内容",
                    )
                if (
                    round_data.usage_result is not None
                    and not isinstance(item, ProviderTurnEnd)
                ):
                    raise _AgentLoopFailure(
                        "invalid_provider_stream",
                        "Provider usage 事件缺失、重复或位置错误",
                    )
                if isinstance(item, ProviderThinkingDelta):
                    round_data.saw_thinking = True
                    yield ModelThinkingEvent(item.text)
                elif isinstance(item, ProviderThinkingComplete):
                    round_data.saw_thinking = True
                    round_data.thinking_blocks.append(item.block)
                elif isinstance(item, ProviderTextDelta):
                    round_data.text_parts.append(item.text)
                    yield ModelTextEvent(item.text)
                elif isinstance(item, ProviderToolCall):
                    round_data.tool_calls.append(item.tool_call)
                elif isinstance(item, ProviderUsageEvent):
                    if round_data.usage_result is not None:
                        raise _AgentLoopFailure(
                            "invalid_provider_stream",
                            (
                                "Provider usage 事件缺失、重复或"
                                "位置错误"
                            ),
                        )
                    round_data.usage_result = item.result
                elif isinstance(item, ProviderTurnEnd):
                    if round_data.usage_result is None:
                        raise _AgentLoopFailure(
                            "invalid_provider_stream",
                            (
                                "Provider usage 事件缺失、重复或"
                                "位置错误"
                            ),
                        )
                    round_data.turn_end = item
                else:
                    raise _AgentLoopFailure(
                        "invalid_provider_stream",
                        "Provider 返回了未知流事件",
                    )

            if round_data.turn_end is None:
                raise _AgentLoopFailure(
                    "invalid_provider_stream",
                    "Provider 流缺少结束事件",
                )
        finally:
            cancel_task.cancel()
            if not producer_task.done():
                producer_task.cancel()
            with suppress(asyncio.CancelledError):
                await producer_task
