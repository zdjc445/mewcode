from __future__ import annotations

import pytest

from mewcode_agent.compaction import (
    CONTEXT_BOUNDARY_TEXT,
    ContextCompactionError,
    ContextProjector,
    SummaryCheckpoint,
    SummarySections,
    VerbatimUserMessage,
    history_atomic_boundaries,
)
from mewcode_agent.models import ChatMessage, ToolCall
from mewcode_agent.prompting.models import (
    ControlMessage,
    ContextBoundaryMessage,
    ContextSummaryMessage,
    PromptFrame,
)


def control(
    identifier: str,
    *,
    scope: str,
    sequence: int,
    anchor: int,
    request: int | None,
    round_number: int | None,
) -> ControlMessage:
    return ControlMessage(
        identifier,
        "context" if scope == "session" else "instruction",
        scope,  # type: ignore[arg-type]
        identifier,
        sequence,
        anchor,
        request,
        round_number,
    )


def sections() -> SummarySections:
    return SummarySections(
        primary_requests=("请求",),
        key_concepts=(),
        files_and_code=(),
        errors_and_fixes=(),
        solution_process=(),
        pending_tasks=(),
        current_work=(),
        next_step=(),
    )


def test_projector_keeps_users_and_active_controls_without_rebasing() -> None:
    session = control(
        "session.control",
        scope="session",
        sequence=1,
        anchor=0,
        request=None,
        round_number=None,
    )
    expired = control(
        "request.old",
        scope="request",
        sequence=2,
        anchor=0,
        request=1,
        round_number=None,
    )
    active = control(
        "request.active",
        scope="request",
        sequence=3,
        anchor=2,
        request=2,
        round_number=None,
    )
    first_user = ChatMessage(role="user", content="第一问")
    first_assistant = ChatMessage(role="assistant", content="第一答")
    second_user = ChatMessage(role="user", content="第二问")
    frame = PromptFrame(
        "system",
        (
            session,
            expired,
            first_user,
            first_assistant,
            active,
            second_user,
        ),
    )
    checkpoint = SummaryCheckpoint(
        1,
        2,
        sections(),
        (VerbatimUserMessage(0, "第一问"),),
    )

    projected = ContextProjector().project(
        frame,
        checkpoint,
        active_request_sequence=2,
        active_round_number=None,
    )

    assert projected.items[0] is session
    assert expired not in projected.items
    assert first_user in projected.items
    assert first_assistant not in projected.items
    summary_index = next(
        index
        for index, item in enumerate(projected.items)
        if isinstance(item, ContextSummaryMessage)
    )
    assert isinstance(projected.items[summary_index + 1], ContextBoundaryMessage)
    boundary = projected.items[summary_index + 1]
    assert isinstance(boundary, ContextBoundaryMessage)
    assert boundary.content == CONTEXT_BOUNDARY_TEXT
    assert projected.items[summary_index + 2] is active
    assert projected.items[-1] is second_user
    assert active.anchor == 2


def test_atomic_boundaries_never_split_tool_exchange() -> None:
    call = ToolCall("call", "read_file", "{}")
    messages = [
        ChatMessage(role="user", content="任务"),
        ChatMessage(role="assistant", content="", tool_calls=(call,)),
        ChatMessage(role="tool", content="{}", tool_call_id="call"),
        ChatMessage(role="assistant", content="完成"),
    ]

    assert history_atomic_boundaries(messages) == (1, 3, 4)


def test_atomic_boundaries_reject_orphan_tool_result() -> None:
    with pytest.raises(ContextCompactionError) as caught:
        history_atomic_boundaries(
            [ChatMessage(role="tool", content="{}", tool_call_id="call")]
        )

    assert caught.value.code == "context_invalid_tool_batch"
