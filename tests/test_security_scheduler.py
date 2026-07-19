from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mewcode_agent.agent import (
    AgentRunContext,
    ToolApprovalRequestedEvent,
    ToolCallStartedEvent,
    ToolResultEvent,
    ToolScheduler,
    ToolSchedulerEvent,
)
from mewcode_agent.models import ToolCall
from mewcode_agent.security import (
    ArgumentMatcher,
    PathSandbox,
    PermanentApprovalStore,
    SecurityBoundary,
    SecurityConfigError,
    SecurityConfiguration,
    SecurityPolicyEngine,
    SecurityRule,
)
from mewcode_agent.tools import Tool, ToolRegistry


class CountingTool(Tool):
    description = "security scheduler test tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name: str, category: str) -> None:
        self.name = name
        self.category = category  # type: ignore[assignment]
        self.executions = 0

    async def execute(self, arguments: dict[str, Any]) -> dict[str, int]:
        self.executions += 1
        return {"executions": self.executions}


class FailingApprovalStore(PermanentApprovalStore):
    def add(self, rule: SecurityRule) -> None:
        raise SecurityConfigError("simulated persistence failure")


def make_scheduler(
    tmp_path: Path,
    tool: CountingTool,
    *,
    mode: str = "default",
    project_rules: tuple[SecurityRule, ...] = (),
    approval_store: PermanentApprovalStore | None = None,
) -> ToolScheduler:
    boundary = SecurityBoundary(PathSandbox(tmp_path))
    policy = SecurityPolicyEngine(
        SecurityConfiguration(
            mode,  # type: ignore[arg-type]
            (),
            project_rules,
        ),
        boundary,
        approval_store=approval_store,
    )
    registry = ToolRegistry(security_boundary=boundary)
    registry.register(tool)
    return ToolScheduler(registry, policy_engine=policy)


async def consume(
    scheduler: ToolScheduler,
    calls: tuple[ToolCall, ...],
    *,
    decisions: list[str],
    authorized: bool = False,
) -> list[ToolSchedulerEvent]:
    context = AgentRunContext()
    context.begin_run()
    remaining = list(decisions)
    events: list[ToolSchedulerEvent] = []
    async for event in scheduler.run(
        calls,
        plan_only=False,
        current_request_authorized=authorized,
        context=context,
    ):
        events.append(event)
        if isinstance(event, ToolApprovalRequestedEvent):
            context.resolve_tool_approval(
                event.request_id,
                remaining.pop(0),  # type: ignore[arg-type]
            )
    return events


@pytest.mark.asyncio
async def test_default_mode_asks_for_write_and_session_allow_is_reused(
    tmp_path: Path,
) -> None:
    tool = CountingTool("write_file", "write")
    scheduler = make_scheduler(tmp_path, tool)
    calls = (
        ToolCall(
            "1",
            "write_file",
            '{"path":"src/app.py","content":"first"}',
        ),
        ToolCall(
            "2",
            "write_file",
            '{"path":"src/app.py","content":"second"}',
        ),
    )

    events = await consume(scheduler, calls, decisions=["allow_session"])

    approvals = [
        event
        for event in events
        if isinstance(event, ToolApprovalRequestedEvent)
    ]
    assert len(approvals) == 1
    assert approvals[0].reason_code == "default_mode_default"
    assert tool.executions == 2


@pytest.mark.asyncio
async def test_allow_once_does_not_authorize_later_matching_call(
    tmp_path: Path,
) -> None:
    tool = CountingTool("write_file", "write")
    scheduler = make_scheduler(tmp_path, tool)
    calls = (
        ToolCall("1", "write_file", '{"path":"same.txt"}'),
        ToolCall("2", "write_file", '{"path":"same.txt"}'),
    )

    events = await consume(
        scheduler,
        calls,
        decisions=["allow_once", "reject"],
    )

    approvals = [
        event
        for event in events
        if isinstance(event, ToolApprovalRequestedEvent)
    ]
    assert len(approvals) == 2
    assert tool.executions == 1


@pytest.mark.asyncio
async def test_strict_mode_asks_before_read(tmp_path: Path) -> None:
    tool = CountingTool("read_file", "read")
    scheduler = make_scheduler(tmp_path, tool, mode="strict")

    events = await consume(
        scheduler,
        (ToolCall("1", "read_file", '{"path":"README.md"}'),),
        decisions=["allow_once"],
    )

    assert isinstance(events[0], ToolApprovalRequestedEvent)
    assert isinstance(events[1], ToolCallStartedEvent)
    assert tool.executions == 1


@pytest.mark.asyncio
async def test_policy_deny_never_emits_started_or_approval(tmp_path: Path) -> None:
    tool = CountingTool("run_command", "command")
    deny = SecurityRule(
        "project.deny_echo",
        "project",
        1,
        "deny",
        "run_command",
        (ArgumentMatcher("command", "glob", "echo*"),),
    )
    scheduler = make_scheduler(tmp_path, tool, project_rules=(deny,))

    events = await consume(
        scheduler,
        (ToolCall("1", "run_command", '{"command":"echo hello"}'),),
        decisions=[],
    )

    assert not any(
        isinstance(event, ToolApprovalRequestedEvent) for event in events
    )
    assert not any(isinstance(event, ToolCallStartedEvent) for event in events)
    result = next(
        event for event in events if isinstance(event, ToolResultEvent)
    )
    assert result.result.error_code == "tool_denied_by_policy"
    assert tool.executions == 0


@pytest.mark.asyncio
async def test_hard_deny_precedes_current_request_authorization(
    tmp_path: Path,
) -> None:
    tool = CountingTool("run_command", "command")
    scheduler = make_scheduler(tmp_path, tool, mode="permissive")

    events = await consume(
        scheduler,
        (
            ToolCall(
                "1",
                "run_command",
                '{"command":"git reset --hard HEAD"}',
            ),
        ),
        decisions=[],
        authorized=True,
    )

    result = next(
        event for event in events if isinstance(event, ToolResultEvent)
    )
    assert result.result.error_code == "tool_denied_by_policy"
    assert tool.executions == 0


@pytest.mark.asyncio
async def test_permanent_allow_is_written_and_used_without_second_prompt(
    tmp_path: Path,
) -> None:
    tool = CountingTool("run_command", "command")
    store = PermanentApprovalStore(tmp_path / "home" / "approvals.yaml")
    scheduler = make_scheduler(tmp_path, tool, approval_store=store)
    call = ToolCall("1", "run_command", '{"command":"echo safe"}')

    first = await consume(
        scheduler,
        (call,),
        decisions=["allow_permanent"],
    )
    second = await consume(
        scheduler,
        (ToolCall("2", call.name, call.arguments_json),),
        decisions=[],
    )

    assert any(isinstance(event, ToolApprovalRequestedEvent) for event in first)
    assert not any(
        isinstance(event, ToolApprovalRequestedEvent) for event in second
    )
    assert tool.executions == 2


@pytest.mark.asyncio
async def test_user_rejection_returns_structured_failure(tmp_path: Path) -> None:
    tool = CountingTool("write_file", "write")
    scheduler = make_scheduler(tmp_path, tool)

    events = await consume(
        scheduler,
        (ToolCall("1", "write_file", '{"path":"new.txt"}'),),
        decisions=["reject"],
    )

    result = next(
        event for event in events if isinstance(event, ToolResultEvent)
    )
    assert result.result.error_code == "tool_denied_by_user"
    assert tool.executions == 0


@pytest.mark.asyncio
async def test_permanent_approval_failure_does_not_execute_tool(
    tmp_path: Path,
) -> None:
    tool = CountingTool("run_command", "command")
    store = FailingApprovalStore(tmp_path / "approvals.yaml")
    scheduler = make_scheduler(tmp_path, tool, approval_store=store)

    events = await consume(
        scheduler,
        (ToolCall("1", "run_command", '{"command":"echo safe"}'),),
        decisions=["allow_permanent"],
    )

    result = next(
        event for event in events if isinstance(event, ToolResultEvent)
    )
    assert result.result.error_code == "security_persistence_failed"
    assert tool.executions == 0
