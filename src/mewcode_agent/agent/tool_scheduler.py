"""Ordered tool scheduling with plan-only approvals and cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol, TypeAlias

from mewcode_agent.agent.context import AgentRunCancelled, AgentRunContext
from mewcode_agent.agent.events import (
    ToolApprovalRequestedEvent,
    ToolCallStartedEvent,
    ToolResultEvent,
)
from mewcode_agent.models import ToolCall
from mewcode_agent.tools.base import Tool, ToolResult
from mewcode_agent.tools.registry import ToolRegistry

ToolSchedulerEvent: TypeAlias = (
    ToolApprovalRequestedEvent | ToolCallStartedEvent | ToolResultEvent
)


class ToolExecutionInterceptor(Protocol):
    async def before_execute(
        self,
        tool_call: ToolCall,
        *,
        plan_only: bool,
        current_request_authorized: bool,
    ) -> ToolResult | None: ...

    async def after_execute(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> ToolResult: ...


class NoOpToolExecutionInterceptor:
    async def before_execute(
        self,
        tool_call: ToolCall,
        *,
        plan_only: bool,
        current_request_authorized: bool,
    ) -> ToolResult | None:
        return None

    async def after_execute(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        return result


class ToolScheduler:
    """Run consecutive reads concurrently and preserve call/result order."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        interceptor: ToolExecutionInterceptor | None = None,
    ) -> None:
        self._registry = registry
        self._interceptor = interceptor or NoOpToolExecutionInterceptor()

    async def run(
        self,
        tool_calls: tuple[ToolCall, ...],
        *,
        plan_only: bool,
        current_request_authorized: bool,
        context: AgentRunContext,
    ) -> AsyncIterator[ToolSchedulerEvent]:
        index = 0
        while index < len(tool_calls):
            if context.cancelled:
                for event in self._cancelled_events(tool_calls[index:]):
                    yield event
                return

            call = tool_calls[index]
            tool = self._registry.get(call.name)
            if tool is None:
                result = await self._registry.execute(
                    call.name,
                    call.arguments_json,
                )
                yield ToolResultEvent(call.call_id, result)
                index += 1
                continue

            if tool.category == "read":
                read_group: list[tuple[ToolCall, Tool]] = []
                group_end = index
                while group_end < len(tool_calls):
                    grouped_call = tool_calls[group_end]
                    grouped_tool = self._registry.get(grouped_call.name)
                    if grouped_tool is None or grouped_tool.category != "read":
                        break
                    read_group.append((grouped_call, grouped_tool))
                    group_end += 1

                for grouped_call, grouped_tool in read_group:
                    yield self._started_event(grouped_call, grouped_tool)
                results = await asyncio.gather(
                    *(
                        self._execute_one(
                            grouped_call,
                            plan_only=plan_only,
                            current_request_authorized=(
                                current_request_authorized
                            ),
                        )
                        for grouped_call, _ in read_group
                    )
                )
                for (grouped_call, _), result in zip(
                    read_group,
                    results,
                    strict=True,
                ):
                    yield ToolResultEvent(grouped_call.call_id, result)
                index = group_end
                continue

            if plan_only and not current_request_authorized:
                request_id = context.open_tool_approval()
                yield ToolApprovalRequestedEvent(
                    request_id=request_id,
                    call_id=call.call_id,
                    tool_name=call.name,
                    arguments_json=call.arguments_json,
                    category=tool.category,
                )
                try:
                    decision = await context.wait_for_tool_approval(request_id)
                except AgentRunCancelled:
                    for event in self._cancelled_events(tool_calls[index:]):
                        yield event
                    return
                if decision == "reject":
                    yield ToolResultEvent(
                        call.call_id,
                        ToolResult(
                            tool_name=call.name,
                            success=False,
                            error_code="tool_blocked_in_plan_mode",
                            error_message=(
                                "工具在 plan-only 模式下被用户拒绝"
                            ),
                        ),
                    )
                    index += 1
                    continue

            if context.cancelled:
                for event in self._cancelled_events(tool_calls[index:]):
                    yield event
                return

            yield self._started_event(call, tool)
            result = await self._execute_one(
                call,
                plan_only=plan_only,
                current_request_authorized=current_request_authorized,
            )
            yield ToolResultEvent(call.call_id, result)
            index += 1

    async def _execute_one(
        self,
        call: ToolCall,
        *,
        plan_only: bool,
        current_request_authorized: bool,
    ) -> ToolResult:
        result = await self._interceptor.before_execute(
            call,
            plan_only=plan_only,
            current_request_authorized=current_request_authorized,
        )
        if result is None:
            result = await self._registry.execute(
                call.name,
                call.arguments_json,
            )
        return await self._interceptor.after_execute(call, result)

    @staticmethod
    def _started_event(
        call: ToolCall,
        tool: Tool,
    ) -> ToolCallStartedEvent:
        return ToolCallStartedEvent(
            call_id=call.call_id,
            tool_name=call.name,
            arguments_json=call.arguments_json,
            category=tool.category,
        )

    @staticmethod
    def _cancelled_events(
        calls: tuple[ToolCall, ...],
    ) -> tuple[ToolResultEvent, ...]:
        return tuple(
            ToolResultEvent(
                call.call_id,
                ToolResult(
                    tool_name=call.name,
                    success=False,
                    error_code="tool_cancelled",
                    error_message="工具因用户取消而未执行",
                ),
            )
            for call in calls
        )
