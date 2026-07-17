from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from mewcode_agent.agent.context import AgentRunContext
from mewcode_agent.agent.events import (
    ToolApprovalRequestedEvent,
    ToolCallStartedEvent,
    ToolResultEvent,
)
from mewcode_agent.agent.tool_scheduler import (
    NoOpToolExecutionInterceptor,
    ToolScheduler,
    ToolSchedulerEvent,
)
from mewcode_agent.models import ToolCall
from mewcode_agent.tools import (
    Tool,
    ToolCategory,
    ToolRegistry,
    ToolResult,
)


class ControlledTool(Tool):
    description = "controlled test tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name: str, category: ToolCategory) -> None:
        self.name = name
        self.category = category
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, arguments: dict[str, Any]) -> dict[str, str]:
        self.started.set()
        await self.release.wait()
        return {"name": self.name}


class TimelineTool(Tool):
    description = "timeline test tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(
        self,
        name: str,
        category: ToolCategory,
        timeline: list[str],
    ) -> None:
        self.name = name
        self.category = category
        self.timeline = timeline

    async def execute(self, arguments: dict[str, Any]) -> dict[str, str]:
        self.timeline.append(f"{self.name}:start")
        await asyncio.sleep(0)
        self.timeline.append(f"{self.name}:end")
        return {"name": self.name}


class RecordingTool(Tool):
    description = "recording test tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name: str, category: ToolCategory) -> None:
        self.name = name
        self.category = category
        self.executions = 0

    async def execute(self, arguments: dict[str, Any]) -> dict[str, int]:
        self.executions += 1
        return {"executions": self.executions}


def register_all(registry: ToolRegistry, *tools: Tool) -> None:
    for tool in tools:
        registry.register(tool)


async def collect_scheduler_events(
    scheduler: ToolScheduler,
    calls: tuple[ToolCall, ...],
    *,
    context: AgentRunContext,
    plan_only: bool = False,
    current_request_authorized: bool = False,
    decisions: list[str] | None = None,
) -> list[ToolSchedulerEvent]:
    remaining_decisions = list(decisions or [])
    events: list[ToolSchedulerEvent] = []
    async for event in scheduler.run(
        calls,
        plan_only=plan_only,
        current_request_authorized=current_request_authorized,
        context=context,
    ):
        events.append(event)
        if isinstance(event, ToolApprovalRequestedEvent):
            context.resolve_tool_approval(
                event.request_id,
                remaining_decisions.pop(0),  # type: ignore[arg-type]
            )
    return events


@pytest.mark.asyncio
async def test_consecutive_reads_run_concurrently_and_keep_result_order() -> None:
    first = ControlledTool("read_1", "read")
    second = ControlledTool("read_2", "read")
    registry = ToolRegistry()
    register_all(registry, first, second)
    scheduler = ToolScheduler(registry)
    context = AgentRunContext()
    context.begin_run()

    task = asyncio.create_task(
        collect_scheduler_events(
            scheduler,
            (
                ToolCall("call_1", "read_1", "{}"),
                ToolCall("call_2", "read_2", "{}"),
            ),
            context=context,
        )
    )
    await first.started.wait()
    await second.started.wait()
    second.release.set()
    first.release.set()
    events = await task

    assert [
        event.call_id
        for event in events
        if isinstance(event, ToolCallStartedEvent)
    ] == ["call_1", "call_2"]
    assert [
        event.call_id
        for event in events
        if isinstance(event, ToolResultEvent)
    ] == ["call_1", "call_2"]


@pytest.mark.asyncio
async def test_write_and_command_are_serial_barriers() -> None:
    timeline: list[str] = []
    registry = ToolRegistry()
    register_all(
        registry,
        TimelineTool("read_1", "read", timeline),
        TimelineTool("read_2", "read", timeline),
        TimelineTool("write_1", "write", timeline),
        TimelineTool("read_3", "read", timeline),
        TimelineTool("command_1", "command", timeline),
    )
    scheduler = ToolScheduler(registry)
    context = AgentRunContext()
    context.begin_run()

    events = await collect_scheduler_events(
        scheduler,
        (
            ToolCall("1", "read_1", "{}"),
            ToolCall("2", "read_2", "{}"),
            ToolCall("3", "write_1", "{}"),
            ToolCall("4", "read_3", "{}"),
            ToolCall("5", "command_1", "{}"),
        ),
        context=context,
    )

    assert timeline.index("read_1:end") < timeline.index("write_1:start")
    assert timeline.index("read_2:end") < timeline.index("write_1:start")
    assert timeline.index("write_1:end") < timeline.index("read_3:start")
    assert timeline.index("read_3:end") < timeline.index("command_1:start")
    assert [
        event.call_id
        for event in events
        if isinstance(event, ToolResultEvent)
    ] == ["1", "2", "3", "4", "5"]


@pytest.mark.asyncio
async def test_unknown_tool_is_an_ordered_barrier_without_started_event() -> None:
    first = RecordingTool("read_1", "read")
    second = RecordingTool("read_2", "read")
    registry = ToolRegistry()
    register_all(registry, first, second)
    context = AgentRunContext()
    context.begin_run()

    events = await collect_scheduler_events(
        ToolScheduler(registry),
        (
            ToolCall("1", "read_1", "{}"),
            ToolCall("2", "missing", "{}"),
            ToolCall("3", "read_2", "{}"),
        ),
        context=context,
    )

    started_ids = [
        event.call_id
        for event in events
        if isinstance(event, ToolCallStartedEvent)
    ]
    result_events = [
        event for event in events if isinstance(event, ToolResultEvent)
    ]
    assert started_ids == ["1", "3"]
    assert [event.call_id for event in result_events] == ["1", "2", "3"]
    assert result_events[1].result.error_code == "tool_not_found"


@pytest.mark.asyncio
async def test_plan_only_approves_each_write_or_command_once() -> None:
    write = RecordingTool("write_1", "write")
    command = RecordingTool("command_1", "command")
    registry = ToolRegistry()
    register_all(registry, write, command)
    context = AgentRunContext()
    context.begin_run()

    events = await collect_scheduler_events(
        ToolScheduler(registry),
        (
            ToolCall("1", "write_1", "{}"),
            ToolCall("2", "command_1", "{}"),
        ),
        context=context,
        plan_only=True,
        decisions=["allow_once", "reject"],
    )

    approvals = [
        event
        for event in events
        if isinstance(event, ToolApprovalRequestedEvent)
    ]
    results = [
        event for event in events if isinstance(event, ToolResultEvent)
    ]
    assert [event.call_id for event in approvals] == ["1", "2"]
    assert write.executions == 1
    assert command.executions == 0
    assert results[1].result.error_code == "tool_blocked_in_plan_mode"
    assert (
        results[1].result.error_message
        == "工具在 plan-only 模式下被用户拒绝"
    )
    assert "2" not in {
        event.call_id
        for event in events
        if isinstance(event, ToolCallStartedEvent)
    }


@pytest.mark.asyncio
async def test_current_request_authorization_skips_plan_only_approvals() -> None:
    write = RecordingTool("write_1", "write")
    command = RecordingTool("command_1", "command")
    registry = ToolRegistry()
    register_all(registry, write, command)
    context = AgentRunContext()
    context.begin_run()

    events = await collect_scheduler_events(
        ToolScheduler(registry),
        (
            ToolCall("1", "write_1", "{}"),
            ToolCall("2", "command_1", "{}"),
        ),
        context=context,
        plan_only=True,
        current_request_authorized=True,
    )

    assert not any(
        isinstance(event, ToolApprovalRequestedEvent) for event in events
    )
    assert write.executions == 1
    assert command.executions == 1


@pytest.mark.asyncio
async def test_cancel_waits_for_started_read_group_and_cancels_remaining() -> None:
    first = ControlledTool("read_1", "read")
    second = ControlledTool("read_2", "read")
    write = RecordingTool("write_1", "write")
    registry = ToolRegistry()
    register_all(registry, first, second, write)
    context = AgentRunContext()
    context.begin_run()

    task = asyncio.create_task(
        collect_scheduler_events(
            ToolScheduler(registry),
            (
                ToolCall("1", "read_1", "{}"),
                ToolCall("2", "read_2", "{}"),
                ToolCall("3", "write_1", "{}"),
            ),
            context=context,
        )
    )
    await first.started.wait()
    await second.started.wait()
    context.cancel()
    first.release.set()
    second.release.set()
    events = await task

    results = [
        event for event in events if isinstance(event, ToolResultEvent)
    ]
    assert [event.call_id for event in results] == ["1", "2", "3"]
    assert results[0].result.success is True
    assert results[1].result.success is True
    assert results[2].result.error_code == "tool_cancelled"
    assert results[2].result.error_message == "工具因用户取消而未执行"
    assert write.executions == 0


@pytest.mark.asyncio
async def test_cancel_during_approval_cancels_current_and_later_calls() -> None:
    write = RecordingTool("write_1", "write")
    command = RecordingTool("command_1", "command")
    registry = ToolRegistry()
    register_all(registry, write, command)
    scheduler = ToolScheduler(registry)
    context = AgentRunContext()
    context.begin_run()
    approval_seen = asyncio.Event()

    async def consume() -> list[ToolSchedulerEvent]:
        events: list[ToolSchedulerEvent] = []
        async for event in scheduler.run(
            (
                ToolCall("1", "write_1", "{}"),
                ToolCall("2", "command_1", "{}"),
            ),
            plan_only=True,
            current_request_authorized=False,
            context=context,
        ):
            events.append(event)
            if isinstance(event, ToolApprovalRequestedEvent):
                approval_seen.set()
        return events

    task = asyncio.create_task(consume())
    await approval_seen.wait()
    context.cancel()
    events = await task

    results = [
        event for event in events if isinstance(event, ToolResultEvent)
    ]
    assert [event.call_id for event in results] == ["1", "2"]
    assert all(event.result.error_code == "tool_cancelled" for event in results)
    assert write.executions == 0
    assert command.executions == 0


class TransformingInterceptor(NoOpToolExecutionInterceptor):
    def __init__(self, *, block_before: bool) -> None:
        self.block_before = block_before

    async def before_execute(
        self,
        tool_call: ToolCall,
        *,
        plan_only: bool,
        current_request_authorized: bool,
    ) -> ToolResult | None:
        if self.block_before:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                error_code="blocked_by_test",
                error_message="blocked",
            )
        return None

    async def after_execute(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        return ToolResult(
            tool_name=tool_call.name,
            success=result.success,
            data={"after": True},
            error_code=result.error_code,
            error_message=result.error_message,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("block_before", [False, True])
async def test_interceptor_can_block_and_transform_results(
    block_before: bool,
) -> None:
    tool = RecordingTool("read_1", "read")
    registry = ToolRegistry()
    registry.register(tool)
    context = AgentRunContext()
    context.begin_run()
    scheduler = ToolScheduler(
        registry,
        interceptor=TransformingInterceptor(block_before=block_before),
    )

    events = await collect_scheduler_events(
        scheduler,
        (ToolCall("1", "read_1", "{}"),),
        context=context,
    )

    result = next(
        event.result for event in events if isinstance(event, ToolResultEvent)
    )
    assert tool.executions == (0 if block_before else 1)
    assert result.data == {"after": True}
    assert result.error_code == ("blocked_by_test" if block_before else None)


@pytest.mark.asyncio
async def test_noop_interceptor_returns_inputs_unchanged() -> None:
    interceptor = NoOpToolExecutionInterceptor()
    call = ToolCall("1", "read_1", "{}")
    result = ToolResult("read_1", True, data={"value": 1})

    assert (
        await interceptor.before_execute(
            call,
            plan_only=False,
            current_request_authorized=False,
        )
        is None
    )
    assert await interceptor.after_execute(call, result) is result
