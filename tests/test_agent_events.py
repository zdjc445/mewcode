from dataclasses import FrozenInstanceError, fields

import pytest

from mewcode_agent.agent.events import (
    FinalResponseEvent,
    PlanApprovalRequestedEvent,
    RoundStartedEvent,
    ToolApprovalRequestedEvent,
)


def test_agent_events_are_frozen_value_objects() -> None:
    event = RoundStartedEvent(1, 15, "planning")

    with pytest.raises(FrozenInstanceError):
        event.round_number = 2  # type: ignore[misc]


def test_approval_events_contain_ids_not_futures() -> None:
    tool_event = ToolApprovalRequestedEvent(
        "approval-1",
        "call-1",
        "write_file",
        "{}",
        "write",
    )
    plan_event = PlanApprovalRequestedEvent(
        "approval-2",
        "计划",
        True,
        True,
    )

    assert tool_event.request_id == "approval-1"
    assert plan_event.request_id == "approval-2"
    assert {field.name for field in fields(tool_event)} == {
        "request_id",
        "call_id",
        "tool_name",
        "arguments_json",
        "category",
    }
    assert {field.name for field in fields(plan_event)} == {
        "request_id",
        "plan",
        "can_execute",
        "can_request_changes",
    }
    assert FinalResponseEvent("完成", 2).total_rounds == 2
