"""UI-independent ReAct loop with structured events and plan approvals."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from mewcode_agent.agent.context import AgentRunCancelled, AgentRunContext
from mewcode_agent.agent.events import (
    AgentEvent,
    AgentRunMode,
    AgentRunState,
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
from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ThinkingBlock, ToolCall
from mewcode_agent.providers.base import (
    LLMProvider,
    ProviderError,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderThinkingComplete,
    ProviderThinkingDelta,
    ProviderToolCall,
    ProviderTurnEnd,
)
from mewcode_agent.tools.registry import ToolRegistry

EXECUTION_PROMPT = """\
You are a coding agent. Use the available tools when needed.
When the task is complete, return a final response without tool calls."""

PLANNING_PROMPT = """\
You are in plan-only mode. Inspect the project with read tools and produce
an implementation plan. Write and command tools require user approval."""

APPROVED_PLAN_PROMPT = """\
The user approved the current plan. Execute it for this request.
The approval expires when this request ends."""

FINAL_ROUND_PROMPT = """\
This is the final allowed model round. Do not request tools.
Return the best final response using the available results."""

APPROVED_PLAN_CONTROL_MESSAGE = "计划已批准，请执行当前计划。"


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
    turn_end: ProviderTurnEnd | None = None


@dataclass(frozen=True, slots=True)
class _ProviderFailure:
    error: Exception


class _AgentLoopFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_PROVIDER_DONE = object()


class AgentLoop:
    """Run one user request through LLM/tool turns."""

    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        *,
        config: AgentLoopConfig | None = None,
        scheduler: ToolScheduler | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._config = config or AgentLoopConfig()
        self._scheduler = scheduler or ToolScheduler(registry)

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
        state: AgentRunState = "planning" if plan_only else "executing"
        current_request_authorized = False
        round_number = 0
        history.add_user(user_message)

        try:
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
                tools = (
                    None
                    if is_final_round
                    else self._registry.api_tools(self._provider.protocol)
                )
                system_prompt = self._system_prompt(
                    mode=mode,
                    current_request_authorized=current_request_authorized,
                    is_final_round=is_final_round,
                )
                round_data = _RoundData()

                try:
                    provider_stream = self._provider.stream_chat(
                        history.snapshot(),
                        tools=tools,
                        system_prompt=system_prompt,
                    )
                    async for event in self._consume_provider_round(
                        provider_stream,
                        context=context,
                        round_data=round_data,
                    ):
                        yield event
                except AgentRunCancelled:
                    state = "cancelled"
                    yield RunCancelledEvent("user_cancelled")
                    return
                except TimeoutError:
                    state = "failed"
                    yield RunErrorEvent(
                        "llm_timeout",
                        (
                            "模型单轮调用超过 "
                            f"{self._config.llm_timeout_seconds:g} 秒"
                        ),
                    )
                    return
                except ProviderError as exc:
                    state = "failed"
                    yield RunErrorEvent("provider_error", str(exc))
                    return
                except _AgentLoopFailure as exc:
                    state = "failed"
                    yield RunErrorEvent(exc.code, exc.message)
                    return
                except Exception:
                    state = "failed"
                    yield RunErrorEvent("provider_error", "模型调用失败")
                    return

                turn_end = round_data.turn_end
                if turn_end is None:
                    state = "failed"
                    yield RunErrorEvent(
                        "invalid_provider_stream",
                        "Provider 流缺少结束事件",
                    )
                    return

                has_tool_calls = bool(round_data.tool_calls)
                if is_final_round and has_tool_calls:
                    state = "failed"
                    yield RunErrorEvent(
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
                    yield RunErrorEvent(
                        "invalid_provider_stream",
                        "工具调用轮缺少完整 thinking 元数据",
                    )
                    return

                if turn_end.stop_reason == "max_tokens":
                    state = "failed"
                    yield RunErrorEvent(
                        "max_tokens_reached",
                        "模型达到 Token 上限，未能完成当前响应",
                    )
                    return

                if has_tool_calls != (
                    turn_end.stop_reason == "tool_calls"
                ):
                    state = "failed"
                    yield RunErrorEvent(
                        "invalid_provider_stream",
                        "Provider 停止原因与工具调用不一致",
                    )
                    return

                if has_tool_calls:
                    history.add_assistant_tool_calls(
                        "".join(round_data.text_parts),
                        tuple(round_data.tool_calls),
                        thinking_blocks=tuple(round_data.thinking_blocks),
                    )
                    async for event in self._scheduler.run(
                        tuple(round_data.tool_calls),
                        plan_only=plan_only,
                        current_request_authorized=(
                            current_request_authorized
                        ),
                        context=context,
                    ):
                        if isinstance(event, ToolResultEvent):
                            history.add_tool_result(event.call_id, event.result)
                        yield event
                    if context.cancelled:
                        state = "cancelled"
                        yield RunCancelledEvent("user_cancelled")
                        return
                    continue

                content = "".join(round_data.text_parts)
                if not content.strip():
                    state = "failed"
                    if round_data.saw_thinking:
                        yield RunErrorEvent(
                            "invalid_provider_stream",
                            "Provider 只返回了 thinking，没有正文",
                        )
                    else:
                        yield RunErrorEvent(
                            "empty_response",
                            "模型没有返回正文、thinking 或工具调用",
                        )
                    return

                if mode == "executing":
                    history.add_assistant(content)
                    state = "completed"
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
                    yield RunCancelledEvent("user_cancelled")
                    return

                if is_final_round:
                    state = "cancelled"
                    yield RunCancelledEvent("round_limit_after_plan")
                    return
                if resolution.decision == "execute_current":
                    history.add_user(APPROVED_PLAN_CONTROL_MESSAGE)
                    current_request_authorized = True
                    state = "executing"
                elif resolution.decision == "request_changes":
                    history.add_user(resolution.feedback)
                    state = "planning"
                    yield UserMessageEvent(resolution.feedback)
                else:
                    state = "cancelled"
                    yield RunCancelledEvent("plan_rejected")
                    return

            state = "failed"
            yield RunErrorEvent(
                "max_rounds_exceeded",
                "当前请求已达到模型轮数上限",
            )
        finally:
            context.finish_run()

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
                elif isinstance(item, ProviderTurnEnd):
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

    @staticmethod
    def _system_prompt(
        *,
        mode: AgentRunMode,
        current_request_authorized: bool,
        is_final_round: bool,
    ) -> str:
        parts = [PLANNING_PROMPT if mode == "planning" else EXECUTION_PROMPT]
        if current_request_authorized:
            parts.append(APPROVED_PLAN_PROMPT)
        if is_final_round:
            parts.append(FINAL_ROUND_PROMPT)
        return "\n".join(parts)
