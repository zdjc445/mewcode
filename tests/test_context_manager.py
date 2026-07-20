from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from mewcode_agent.compaction import (
    CompactionConfig,
    ContextCompactionError,
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

    async def compact(
        self,
        history: ConversationHistory,
    ) -> ToolCompactionResult:
        self.calls += 1
        return ToolCompactionResult(0, 0, 0, 0)


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
    ) -> SummaryGeneration:
        self.calls.append((history_start, history_end))
        if self.fail:
            raise ContextCompactionError(
                "context_summary_failed",
                "测试摘要失败",
            )
        return SummaryGeneration(
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
