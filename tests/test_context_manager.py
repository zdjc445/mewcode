from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from mewcode_agent.compaction import (
    CompactionConfig,
    ContextCompactionError,
    ContextEstimate,
    ContextPreparation,
    ContextWindowManager,
    SummaryGeneration,
    SummarySections,
    ToolCompactionResult,
)
from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage
from mewcode_agent.prompting.models import (
    ContextBoundaryMessage,
    ContextSummaryMessage,
    PromptFrame,
)
from mewcode_agent.providers.base import (
    ProviderRequest,
    ProviderUsageResult,
)


class MeasureProvider:
    provider_id = "measure"
    protocol = "openai"

    def prompt_payload(self, request: ProviderRequest) -> dict[str, Any]:
        messages: list[dict[str, object]] = [
            {"role": "system", "content": request.system_prompt}
        ]
        for item in request.items:
            if isinstance(item, ChatMessage):
                messages.append({"role": item.role, "content": item.content})
            elif isinstance(item, ContextSummaryMessage):
                messages.append(
                    {"role": "system", "content": item.content_json}
                )
            elif isinstance(item, ContextBoundaryMessage):
                messages.append(
                    {"role": "system", "content": item.content}
                )
        return {"model": "test", "messages": messages}


@dataclass
class StubToolCompactor:
    calls: int = 0
    reset_calls: int = 0

    async def compact(
        self,
        history: ConversationHistory,
    ) -> ToolCompactionResult:
        self.calls += 1
        return ToolCompactionResult(0, 0, 0, 0)

    def reset_session(self) -> None:
        self.reset_calls += 1


class StubSummarizer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[int, int]] = []

    async def summarize(
        self,
        *,
        previous: object,
        history_start: int,
        history_end: int,
        messages: tuple[ChatMessage, ...],
        on_usage: Callable[[ProviderUsageResult], None] | None = None,
    ) -> SummaryGeneration:
        self.calls.append((history_start, history_end))
        if self.fail:
            raise ContextCompactionError(
                "context_summary_failed",
                "测试摘要失败",
            )
        result = SummaryGeneration(
            SummarySections(
                primary_requests=("保留请求",),
                key_concepts=(),
                files_and_code=(),
                errors_and_fixes=(),
                solution_process=(),
                pending_tasks=(),
                current_work=(),
                next_step=(),
            ),
            ProviderUsageResult("unavailable", None, "test"),
        )
        if on_usage is not None:
            on_usage(result.usage_result)
        return result


class SequencedEstimator:
    def __init__(self, values: tuple[int, ...]) -> None:
        self._values = list(values)

    def estimate(
        self,
        provider: object,
        request: ProviderRequest,
    ) -> ContextEstimate:
        del provider, request
        return ContextEstimate(self._values.pop(0), False)

    def record_usage(
        self,
        provider: object,
        request: ProviderRequest,
        result: ProviderUsageResult,
    ) -> None:
        del provider, request, result

    def reset_session(self) -> None:
        return None


def manager_config() -> CompactionConfig:
    return CompactionConfig(
        summary_response_bytes=64,
        framing_safety_tokens=10,
        auto_trigger_ratio=0.5,
        target_ratio=0.4,
        max_summary_failures=3,
    )


def populated_history() -> ConversationHistory:
    history = ConversationHistory()
    history.add_user("第一问  ")
    history.add_assistant("a" * 800)
    history.add_user("第二问")
    history.add_assistant("b" * 800)
    return history


def frame_factory(history: ConversationHistory):
    return lambda: PromptFrame("system", tuple(history.snapshot()))


def make_manager(
    summarizer: StubSummarizer,
    compactor: StubToolCompactor,
) -> ContextWindowManager:
    return ContextWindowManager(
        MeasureProvider(),  # type: ignore[arg-type]
        compactor,  # type: ignore[arg-type]
        summarizer,  # type: ignore[arg-type]
        context_window_tokens=3000,
        max_tokens=100,
        config=manager_config(),
    )


def test_reset_session_clears_checkpoint_failures_and_compactor_state() -> None:
    summarizer = StubSummarizer()
    compactor = StubToolCompactor()
    manager = make_manager(summarizer, compactor)
    manager._consecutive_summary_failures = 2
    manager._auto_compaction_disabled = True
    manager._auto_warning_emitted = True
    manager._last_auto_attempt_request_sequence = 8

    manager.reset_session()

    assert manager.checkpoint is None
    assert manager.consecutive_summary_failures == 0
    assert manager.auto_compaction_disabled is False
    assert compactor.reset_calls == 1


def test_status_estimate_is_read_only_and_reports_compaction_state() -> None:
    summarizer = StubSummarizer()
    compactor = StubToolCompactor()
    estimator = SequencedEstimator((1234,))
    manager = ContextWindowManager(
        MeasureProvider(),  # type: ignore[arg-type]
        compactor,  # type: ignore[arg-type]
        summarizer,  # type: ignore[arg-type]
        context_window_tokens=3000,
        max_tokens=100,
        estimator=estimator,  # type: ignore[arg-type]
        config=manager_config(),
    )
    manager._consecutive_summary_failures = 2

    status = manager.inspect_status(
        PromptFrame("system", (ChatMessage(role="user", content="hello"),)),
        tools=None,
    )

    assert status.estimated_prompt_tokens == 1234
    assert status.used_actual_baseline is False
    assert status.prompt_budget_tokens == 2900
    assert status.auto_trigger_tokens == 1450
    assert status.checkpoint_generation == 0
    assert status.checkpoint_covered_messages == 0
    assert status.consecutive_summary_failures == 2
    assert status.auto_compaction_disabled is False
    assert compactor.calls == 0
    assert summarizer.calls == []


@pytest.mark.asyncio
async def test_restored_history_below_budget_only_runs_layer_one() -> None:
    history = populated_history()
    summarizer = StubSummarizer()
    compactor = StubToolCompactor()
    manager = ContextWindowManager(
        MeasureProvider(),  # type: ignore[arg-type]
        compactor,  # type: ignore[arg-type]
        summarizer,  # type: ignore[arg-type]
        context_window_tokens=3000,
        max_tokens=100,
        estimator=SequencedEstimator((2800,)),  # type: ignore[arg-type]
        config=manager_config(),
    )

    result = await manager.prepare_restored_history(
        history,
        compose_frame=frame_factory(history),
        tools=None,
    )

    assert result.estimate_before == 2800
    assert result.estimate_after == 2800
    assert result.summary_changed is False
    assert compactor.calls == 1
    assert summarizer.calls == []


@pytest.mark.asyncio
async def test_restored_history_at_budget_runs_one_summary_attempt() -> None:
    history = populated_history()
    summarizer = StubSummarizer()
    compactor = StubToolCompactor()
    manager = ContextWindowManager(
        MeasureProvider(),  # type: ignore[arg-type]
        compactor,  # type: ignore[arg-type]
        summarizer,  # type: ignore[arg-type]
        context_window_tokens=3000,
        max_tokens=100,
        estimator=SequencedEstimator(  # type: ignore[arg-type]
            (2900, 2900, 1000, 900)
        ),
        config=manager_config(),
    )

    result = await manager.prepare_restored_history(
        history,
        compose_frame=frame_factory(history),
        tools=None,
    )

    assert result.estimate_before == 2900
    assert result.estimate_after == 900
    assert result.summary_changed is True
    assert compactor.calls == 2
    assert len(summarizer.calls) == 1


@pytest.mark.asyncio
async def test_manager_runs_layer_one_then_commits_summary_projection() -> None:
    history = populated_history()
    summarizer = StubSummarizer()
    compactor = StubToolCompactor()
    manager = make_manager(summarizer, compactor)

    prepared = await manager.prepare_agent_request(
        history,
        compose_frame=frame_factory(history),
        tools=None,
        active_request_sequence=None,
        active_round_number=None,
    )

    assert isinstance(prepared, ContextPreparation)
    assert compactor.calls == 1
    assert summarizer.calls == [(0, 4)]
    assert prepared.checkpoint_changed is True
    assert prepared.estimate_after < prepared.estimate_before
    assert manager.checkpoint is not None
    assert manager.checkpoint.covered_history_end == 4
    assert [
        item.content
        for item in manager.checkpoint.user_messages_verbatim
    ] == ["第一问  ", "第二问"]
    assert [
        item.content
        for item in prepared.request.items
        if isinstance(item, ChatMessage) and item.role == "user"
    ] == ["第一问  ", "第二问"]
    assert not any(
        isinstance(item, ChatMessage) and item.role == "assistant"
        for item in prepared.request.items
    )


@pytest.mark.asyncio
async def test_manager_attempts_auto_summary_once_per_request() -> None:
    history = ConversationHistory()
    history.add_assistant("旧回复")
    summarizer = StubSummarizer()
    manager = ContextWindowManager(
        MeasureProvider(),  # type: ignore[arg-type]
        StubToolCompactor(),  # type: ignore[arg-type]
        summarizer,  # type: ignore[arg-type]
        context_window_tokens=110,
        max_tokens=10,
        estimator=SequencedEstimator((80, 50, 50, 80)),  # type: ignore[arg-type]
    )

    first = await manager.prepare_agent_request(
        history,
        compose_frame=frame_factory(history),
        tools=None,
        active_request_sequence=1,
        active_round_number=1,
    )
    second = await manager.prepare_agent_request(
        history,
        compose_frame=frame_factory(history),
        tools=None,
        active_request_sequence=1,
        active_round_number=2,
    )

    assert first.checkpoint_changed is True
    assert second.checkpoint_changed is False
    assert summarizer.calls == [(0, 1)]


@pytest.mark.asyncio
async def test_three_auto_failures_open_fuse_and_stop_retrying() -> None:
    history = populated_history()
    summarizer = StubSummarizer(fail=True)
    compactor = StubToolCompactor()
    manager = make_manager(summarizer, compactor)
    warnings: list[str | None] = []

    for _ in range(3):
        prepared = await manager.prepare_agent_request(
            history,
            compose_frame=frame_factory(history),
            tools=None,
            active_request_sequence=None,
            active_round_number=None,
        )
        warnings.append(prepared.warning_code)

    after_fuse = await manager.prepare_agent_request(
        history,
        compose_frame=frame_factory(history),
        tools=None,
        active_request_sequence=None,
        active_round_number=None,
    )

    assert len(summarizer.calls) == 3
    assert warnings == [
        "context_summary_failed",
        "context_summary_failed",
        "context_auto_compaction_disabled",
    ]
    assert manager.auto_compaction_disabled is True
    assert after_fuse.warning_code is None
    assert len(summarizer.calls) == 3


@pytest.mark.asyncio
async def test_manual_compaction_bypasses_and_resets_fuse() -> None:
    history = populated_history()
    summarizer = StubSummarizer(fail=True)
    manager = make_manager(summarizer, StubToolCompactor())

    for _ in range(3):
        await manager.prepare_agent_request(
            history,
            compose_frame=frame_factory(history),
            tools=None,
            active_request_sequence=None,
            active_round_number=None,
        )
    summarizer.fail = False

    result = await manager.compact_now(
        history,
        compose_frame=frame_factory(history),
        tools=None,
    )

    assert result.changed is True
    assert manager.auto_compaction_disabled is False
    assert manager.consecutive_summary_failures == 0


@pytest.mark.asyncio
async def test_manual_compaction_awaits_summary_start_callback() -> None:
    history = populated_history()
    summarizer = StubSummarizer()
    manager = make_manager(summarizer, StubToolCompactor())
    calls: list[tuple[int, int, int]] = []

    async def on_start(
        generation: int,
        covered_messages: int,
        estimate_before: int,
    ) -> None:
        assert summarizer.calls == []
        calls.append((generation, covered_messages, estimate_before))

    result = await manager.compact_now(
        history,
        compose_frame=frame_factory(history),
        tools=None,
        on_summary_start=on_start,
    )

    assert result.changed is True
    assert calls == [(1, 4, result.estimate_before)]


@pytest.mark.asyncio
async def test_manual_compaction_is_noop_without_enough_history() -> None:
    history = ConversationHistory()
    history.add_user("任务")
    manager = make_manager(StubSummarizer(), StubToolCompactor())

    result = await manager.compact_now(
        history,
        compose_frame=frame_factory(history),
        tools=None,
    )

    assert result.changed is False
    assert result.generation == 0
