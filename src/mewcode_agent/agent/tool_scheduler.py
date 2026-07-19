"""Ordered tool scheduling with plan-only approvals and cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from typing import Protocol, TypeAlias

from mewcode_agent.agent.context import AgentRunCancelled, AgentRunContext
from mewcode_agent.agent.events import (
    ToolApprovalRequestedEvent,
    ToolCallStartedEvent,
    ToolResultEvent,
)
from mewcode_agent.models import ToolCall
from mewcode_agent.security.models import (
    PolicyDecision,
    SecurityRequest,
)
from mewcode_agent.security.policy import SecurityPolicyEngine
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
        policy_engine: SecurityPolicyEngine | None = None,
    ) -> None:
        self._registry = registry
        self._interceptor = interceptor or NoOpToolExecutionInterceptor()
        self._policy_engine = policy_engine

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

                authorized_reads: list[tuple[ToolCall, Tool]] = []
                blocked_results: dict[str, ToolResult] = {}
                for grouped_call, grouped_tool in read_group:
                    security = self._evaluate_security(
                        grouped_call,
                        grouped_tool,
                        current_request_authorized=current_request_authorized,
                    )
                    if security is None:
                        authorized_reads.append((grouped_call, grouped_tool))
                        continue
                    request, policy_decision = security
                    if policy_decision.action == "deny":
                        blocked_results[grouped_call.call_id] = (
                            self._policy_denied_result(
                                grouped_call,
                                policy_decision,
                            )
                        )
                        continue
                    if policy_decision.action == "allow":
                        authorized_reads.append((grouped_call, grouped_tool))
                        continue

                    approval_id = context.open_tool_approval()
                    yield self._approval_event(
                        approval_id,
                        grouped_call,
                        grouped_tool,
                        policy_decision,
                    )
                    try:
                        approval_result = await self._resolve_approval(
                            context,
                            approval_id,
                            request,
                        )
                    except AgentRunCancelled:
                        for event in self._cancelled_events(
                            tool_calls[index:]
                        ):
                            yield event
                        return
                    if approval_result is None:
                        authorized_reads.append((grouped_call, grouped_tool))
                    else:
                        blocked_results[grouped_call.call_id] = approval_result

                if context.cancelled:
                    for event in self._cancelled_events(tool_calls[index:]):
                        yield event
                    return
                for grouped_call, grouped_tool in authorized_reads:
                    yield self._started_event(grouped_call, grouped_tool)
                executed_results = await asyncio.gather(
                    *(
                        self._execute_one(
                            grouped_call,
                            plan_only=plan_only,
                            current_request_authorized=(
                                current_request_authorized
                            ),
                        )
                        for grouped_call, _ in authorized_reads
                    )
                )
                executed_by_call = {
                    grouped_call.call_id: result
                    for (grouped_call, _), result in zip(
                        authorized_reads,
                        executed_results,
                        strict=True,
                    )
                }
                for grouped_call, _ in read_group:
                    result = blocked_results.get(
                        grouped_call.call_id,
                        executed_by_call.get(grouped_call.call_id),
                    )
                    assert result is not None
                    yield ToolResultEvent(grouped_call.call_id, result)
                index = group_end
                continue

            security = self._evaluate_security(
                call,
                tool,
                current_request_authorized=current_request_authorized,
            )
            if security is not None:
                request, policy_decision = security
                if policy_decision.action == "deny":
                    yield ToolResultEvent(
                        call.call_id,
                        self._policy_denied_result(call, policy_decision),
                    )
                    index += 1
                    continue
                if policy_decision.action == "ask":
                    request_id = context.open_tool_approval()
                    yield self._approval_event(
                        request_id,
                        call,
                        tool,
                        policy_decision,
                    )
                    try:
                        approval_result = await self._resolve_approval(
                            context,
                            request_id,
                            request,
                        )
                    except AgentRunCancelled:
                        for event in self._cancelled_events(
                            tool_calls[index:]
                        ):
                            yield event
                        return
                    if approval_result is not None:
                        yield ToolResultEvent(call.call_id, approval_result)
                        index += 1
                        continue
            elif plan_only and not current_request_authorized:
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

    def _evaluate_security(
        self,
        call: ToolCall,
        tool: Tool,
        *,
        current_request_authorized: bool,
    ) -> tuple[SecurityRequest, PolicyDecision] | None:
        if self._policy_engine is None:
            return None
        try:
            arguments = json.loads(call.arguments_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(arguments, dict):
            return None
        request = SecurityRequest(
            call.call_id,
            call.name,
            tool.category,
            arguments,
            self._policy_engine.boundary.path_sandbox.working_directory,
            current_request_authorized,
        )
        return request, self._policy_engine.evaluate(request)

    async def _resolve_approval(
        self,
        context: AgentRunContext,
        request_id: str,
        request: SecurityRequest,
    ) -> ToolResult | None:
        decision = await context.wait_for_tool_approval(request_id)
        if decision == "reject":
            return ToolResult(
                tool_name=request.tool_name,
                success=False,
                error_code="tool_denied_by_user",
                error_message="工具调用被用户拒绝",
            )
        assert self._policy_engine is not None
        if decision == "allow_session":
            self._policy_engine.allow_for_session(request)
        elif decision == "allow_permanent":
            try:
                await asyncio.to_thread(
                    self._policy_engine.allow_permanently,
                    request,
                )
            except (RuntimeError, OSError):
                return ToolResult(
                    tool_name=request.tool_name,
                    success=False,
                    error_code="security_persistence_failed",
                    error_message="永久审批保存失败，工具未执行",
                )
        return None

    @staticmethod
    def _approval_event(
        request_id: str,
        call: ToolCall,
        tool: Tool,
        decision: PolicyDecision,
    ) -> ToolApprovalRequestedEvent:
        return ToolApprovalRequestedEvent(
            request_id=request_id,
            call_id=call.call_id,
            tool_name=call.name,
            arguments_json=call.arguments_json,
            category=tool.category,
            reason_code=decision.reason_code,
        )

    @staticmethod
    def _policy_denied_result(
        call: ToolCall,
        decision: PolicyDecision,
    ) -> ToolResult:
        return ToolResult(
            tool_name=call.name,
            success=False,
            error_code="tool_denied_by_policy",
            error_message=(
                "工具调用被安全策略拒绝: " f"{decision.reason_code}"
            ),
        )

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
