from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from typing import Any

import pytest

from mewcode_agent.agent import (
    AgentEvent,
    AgentLoop,
    AgentLoopConfig,
    AgentRunContext,
    FinalResponseEvent,
    ModelTextEvent,
    ModelThinkingEvent,
    PlanApprovalRequestedEvent,
    RoundStartedEvent,
    RunCancelledEvent,
    RunErrorEvent,
    ToolApprovalRequestedEvent,
    ToolCallStartedEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from mewcode_agent.agent.loop import (
    APPROVED_PLAN_PROMPT,
    EXECUTION_PROMPT,
    FINAL_ROUND_PROMPT,
    PLANNING_PROMPT,
)
from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall
from mewcode_agent.providers.base import (
    ProviderError,
    ProviderProtocol,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderThinkingComplete,
    ProviderThinkingDelta,
    ProviderToolCall,
    ProviderTurnEnd,
)
from mewcode_agent.tools import Tool, ToolRegistry


class ScriptedProvider:
    protocol: ProviderProtocol = "openai"

    def __init__(
        self,
        rounds: list[list[ProviderStreamEvent] | Exception],
    ) -> None:
        self.rounds = rounds
        self.requests: list[list[ChatMessage]] = []
        self.tools: list[list[dict[str, Any]] | None] = []
        self.system_prompts: list[str] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str,
    ) -> AsyncIterator[ProviderStreamEvent]:
        index = len(self.requests)
        self.requests.append(messages)
        self.tools.append(tools)
        self.system_prompts.append(system_prompt)
        scripted = self.rounds[index]
        if isinstance(scripted, Exception):
            raise scripted
        for event in scripted:
            yield event


class SlowProvider:
    protocol: ProviderProtocol = "openai"

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str,
    ) -> AsyncIterator[ProviderStreamEvent]:
        await asyncio.sleep(1)
        yield ProviderTurnEnd("end_turn")


class EchoReadTool(Tool):
    name = "echo_read"
    description = "Return the input value"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
    }
    category = "read"

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.executions += 1
        return {"value": arguments["value"]}


class RecordingWriteTool(Tool):
    name = "record_write"
    description = "Record one write"
    parameters = {"type": "object", "properties": {}}
    category = "write"

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, arguments: dict[str, Any]) -> dict[str, int]:
        self.executions += 1
        return {"executions": self.executions}


def make_registry(
    *,
    read: EchoReadTool | None = None,
    write: RecordingWriteTool | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(read or EchoReadTool())
    if write is not None:
        registry.register(write)
    return registry


async def collect_run(
    loop: AgentLoop,
    message: str,
    history: ConversationHistory,
    *,
    plan_only: bool = False,
    context: AgentRunContext | None = None,
) -> list[AgentEvent]:
    run_context = context or AgentRunContext()
    return [
        event
        async for event in loop.run(
            message,
            history,
            plan_only=plan_only,
            context=run_context,
        )
    ]


async def collect_with_plan_decisions(
    loop: AgentLoop,
    history: ConversationHistory,
    context: AgentRunContext,
    decisions: list[tuple[str, str]],
) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    async for event in loop.run(
        "规划任务",
        history,
        plan_only=True,
        context=context,
    ):
        events.append(event)
        if isinstance(event, PlanApprovalRequestedEvent):
            decision, feedback = decisions.pop(0)
            context.resolve_plan_approval(
                event.request_id,
                decision,  # type: ignore[arg-type]
                feedback=feedback,
            )
    return events


def terminal_events(events: list[AgentEvent]) -> list[AgentEvent]:
    return [
        event
        for event in events
        if isinstance(
            event,
            (FinalResponseEvent, RunErrorEvent, RunCancelledEvent),
        )
    ]


@pytest.mark.parametrize(
    "config",
    [
        AgentLoopConfig(max_rounds=1),
        AgentLoopConfig(llm_timeout_seconds=0.01),
    ],
)
def test_agent_loop_config_accepts_positive_values(config: AgentLoopConfig) -> None:
    assert config.max_rounds > 0
    assert config.llm_timeout_seconds > 0


@pytest.mark.parametrize(
    "kwargs",
    [{"max_rounds": 0}, {"llm_timeout_seconds": 0}],
)
def test_agent_loop_config_rejects_non_positive_values(
    kwargs: dict[str, float],
) -> None:
    with pytest.raises(ValueError):
        AgentLoopConfig(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_blank_user_message_is_rejected_before_context_is_used() -> None:
    context = AgentRunContext()
    loop = AgentLoop(
        ScriptedProvider([[ProviderTurnEnd("end_turn")]]),
        make_registry(),
    )

    with pytest.raises(ValueError, match="user_message"):
        await collect_run(
            loop,
            "  ",
            ConversationHistory(),
            context=context,
        )

    context.begin_run()


@pytest.mark.asyncio
async def test_one_round_text_response_commits_history_before_final() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderThinkingDelta("分析"),
                ProviderThinkingComplete(ThinkingBlock("分析")),
                ProviderTextDelta("完成"),
                ProviderTurnEnd("end_turn"),
            ]
        ]
    )
    history = ConversationHistory()
    loop = AgentLoop(provider, make_registry())

    events = await collect_run(loop, "任务", history)

    assert events == [
        UserMessageEvent("任务"),
        RoundStartedEvent(1, 15, "executing"),
        ModelThinkingEvent("分析"),
        ModelTextEvent("完成"),
        FinalResponseEvent("完成", 1),
    ]
    assert history.snapshot() == [
        ChatMessage(role="user", content="任务"),
        ChatMessage(role="assistant", content="完成"),
    ]
    assert terminal_events(events) == [FinalResponseEvent("完成", 1)]
    assert events[-1] == FinalResponseEvent("完成", 1)


@pytest.mark.asyncio
async def test_tool_round_commits_thinking_result_then_calls_model_again() -> None:
    call = ToolCall("call_1", "echo_read", '{"value":7}')
    provider = ScriptedProvider(
        [
            [
                ProviderThinkingDelta("需要读取"),
                ProviderThinkingComplete(ThinkingBlock("需要读取")),
                ProviderToolCall(call),
                ProviderTurnEnd("tool_calls"),
            ],
            [ProviderTextDelta("值是 7"), ProviderTurnEnd("end_turn")],
        ]
    )
    history = ConversationHistory()
    loop = AgentLoop(provider, make_registry())

    events = await collect_run(loop, "读取值", history)

    assert [type(event) for event in events] == [
        UserMessageEvent,
        RoundStartedEvent,
        ModelThinkingEvent,
        ToolCallStartedEvent,
        ToolResultEvent,
        RoundStartedEvent,
        ModelTextEvent,
        FinalResponseEvent,
    ]
    tool_message = history.snapshot()[1]
    assert tool_message.tool_calls == (call,)
    assert tool_message.content == ""
    assert tool_message.thinking_blocks == (ThinkingBlock("需要读取"),)
    assert provider.requests[1] == history.snapshot()[:3]
    assert history.snapshot()[-1] == ChatMessage(
        role="assistant",
        content="值是 7",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_events", "expected_code"),
    [
        ([], "invalid_provider_stream"),
        ([ProviderTurnEnd("end_turn")], "empty_response"),
        (
            [
                ProviderThinkingDelta("只有分析"),
                ProviderThinkingComplete(ThinkingBlock("只有分析")),
                ProviderTurnEnd("end_turn"),
            ],
            "invalid_provider_stream",
        ),
        (
            [
                ProviderToolCall(ToolCall("1", "echo_read", "{}")),
                ProviderTurnEnd("end_turn"),
            ],
            "invalid_provider_stream",
        ),
        (
            [ProviderTextDelta("正文"), ProviderTurnEnd("tool_calls")],
            "invalid_provider_stream",
        ),
        (
            [ProviderTextDelta("未完成"), ProviderTurnEnd("max_tokens")],
            "max_tokens_reached",
        ),
        (
            [
                ProviderTurnEnd("end_turn"),
                ProviderTextDelta("结尾后事件"),
            ],
            "invalid_provider_stream",
        ),
    ],
)
async def test_invalid_provider_streams_return_one_terminal_error(
    provider_events: list[ProviderStreamEvent],
    expected_code: str,
) -> None:
    history = ConversationHistory()
    events = await collect_run(
        AgentLoop(ScriptedProvider([provider_events]), make_registry()),
        "任务",
        history,
    )

    assert len(terminal_events(events)) == 1
    error = terminal_events(events)[0]
    assert isinstance(error, RunErrorEvent)
    assert error.code == expected_code
    assert events[-1] is error
    assert history.snapshot() == [ChatMessage(role="user", content="任务")]


@pytest.mark.asyncio
async def test_provider_error_is_safely_exposed() -> None:
    events = await collect_run(
        AgentLoop(
            ScriptedProvider([ProviderError("已脱敏错误")]),
            make_registry(),
        ),
        "任务",
        ConversationHistory(),
    )

    assert events[-1] == RunErrorEvent("provider_error", "已脱敏错误")


@pytest.mark.asyncio
async def test_llm_round_timeout_returns_terminal_error() -> None:
    loop = AgentLoop(
        SlowProvider(),
        make_registry(),
        config=AgentLoopConfig(llm_timeout_seconds=0.01),
    )

    events = await collect_run(loop, "任务", ConversationHistory())

    assert isinstance(events[-1], RunErrorEvent)
    assert events[-1].code == "llm_timeout"


@pytest.mark.asyncio
async def test_cancel_during_provider_stream_discards_partial_assistant() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderTextDelta("临时正文"),
                ProviderTurnEnd("end_turn"),
            ]
        ]
    )
    history = ConversationHistory()
    context = AgentRunContext()
    events: list[AgentEvent] = []

    async for event in AgentLoop(provider, make_registry()).run(
        "任务",
        history,
        plan_only=False,
        context=context,
    ):
        events.append(event)
        if isinstance(event, ModelTextEvent):
            context.cancel()

    assert events[-1] == RunCancelledEvent("user_cancelled")
    assert history.snapshot() == [ChatMessage(role="user", content="任务")]


@pytest.mark.asyncio
async def test_fifteenth_round_disables_tools_and_returns_final_text() -> None:
    rounds: list[list[ProviderStreamEvent]] = []
    for number in range(1, 15):
        rounds.append(
            [
                ProviderToolCall(
                    ToolCall(
                        f"call_{number}",
                        "echo_read",
                        json.dumps({"value": number}),
                    )
                ),
                ProviderTurnEnd("tool_calls"),
            ]
        )
    rounds.append(
        [ProviderTextDelta("最终总结"), ProviderTurnEnd("end_turn")]
    )
    provider = ScriptedProvider(rounds)

    events = await collect_run(
        AgentLoop(provider, make_registry()),
        "长任务",
        ConversationHistory(),
    )

    assert len(provider.requests) == 15
    assert all(tools is not None for tools in provider.tools[:14])
    assert provider.tools[14] is None
    assert FINAL_ROUND_PROMPT in provider.system_prompts[14]
    assert events[-1] == FinalResponseEvent("最终总结", 15)


@pytest.mark.asyncio
async def test_tool_call_on_final_round_is_not_executed() -> None:
    read = EchoReadTool()
    provider = ScriptedProvider(
        [
            [
                ProviderToolCall(
                    ToolCall("call_1", "echo_read", '{"value":1}')
                ),
                ProviderTurnEnd("tool_calls"),
            ]
        ]
    )
    loop = AgentLoop(
        provider,
        make_registry(read=read),
        config=AgentLoopConfig(max_rounds=1),
    )

    events = await collect_run(loop, "任务", ConversationHistory())

    assert events[-1].code == "max_rounds_exceeded"  # type: ignore[union-attr]
    assert read.executions == 0
    assert provider.tools == [None]


@pytest.mark.asyncio
async def test_final_round_tool_call_wins_over_stop_reason_conflict() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderToolCall(
                    ToolCall("call_1", "echo_read", '{"value":1}')
                ),
                ProviderTurnEnd("end_turn"),
            ]
        ]
    )
    events = await collect_run(
        AgentLoop(
            provider,
            make_registry(),
            config=AgentLoopConfig(max_rounds=1),
        ),
        "任务",
        ConversationHistory(),
    )

    assert isinstance(events[-1], RunErrorEvent)
    assert events[-1].code == "max_rounds_exceeded"


@pytest.mark.asyncio
async def test_tool_round_rejects_incomplete_thinking_metadata() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderThinkingDelta("只有分片"),
                ProviderToolCall(
                    ToolCall("call_1", "echo_read", '{"value":1}')
                ),
                ProviderTurnEnd("tool_calls"),
            ]
        ]
    )
    history = ConversationHistory()
    events = await collect_run(
        AgentLoop(provider, make_registry()),
        "任务",
        history,
    )

    assert isinstance(events[-1], RunErrorEvent)
    assert events[-1].code == "invalid_provider_stream"
    assert history.snapshot() == [ChatMessage(role="user", content="任务")]


@pytest.mark.asyncio
async def test_approved_plan_executes_with_request_scoped_authorization() -> None:
    write = RecordingWriteTool()
    provider = ScriptedProvider(
        [
            [ProviderTextDelta("执行计划"), ProviderTurnEnd("end_turn")],
            [
                ProviderToolCall(ToolCall("write_1", "record_write", "{}")),
                ProviderTurnEnd("tool_calls"),
            ],
            [ProviderTextDelta("执行完成"), ProviderTurnEnd("end_turn")],
        ]
    )
    history = ConversationHistory()
    context = AgentRunContext()
    events = await collect_with_plan_decisions(
        AgentLoop(provider, make_registry(write=write)),
        history,
        context,
        [("execute_current", "")],
    )

    assert write.executions == 1
    assert not any(
        isinstance(event, ToolApprovalRequestedEvent) for event in events
    )
    assert history.snapshot()[2] == ChatMessage(
        role="user",
        content="计划已批准，请执行当前计划。",
    )
    assert UserMessageEvent("计划已批准，请执行当前计划。") not in events
    assert provider.system_prompts[0] == PLANNING_PROMPT
    assert provider.system_prompts[1] == (
        EXECUTION_PROMPT + "\n" + APPROVED_PLAN_PROMPT
    )
    assert events[-1] == FinalResponseEvent("执行完成", 3)


@pytest.mark.asyncio
async def test_plan_authorization_expires_before_next_request() -> None:
    write = RecordingWriteTool()
    provider = ScriptedProvider(
        [
            [ProviderTextDelta("第一份计划"), ProviderTurnEnd("end_turn")],
            [
                ProviderToolCall(ToolCall("write_1", "record_write", "{}")),
                ProviderTurnEnd("tool_calls"),
            ],
            [ProviderTextDelta("第一次完成"), ProviderTurnEnd("end_turn")],
            [
                ProviderToolCall(ToolCall("write_2", "record_write", "{}")),
                ProviderTurnEnd("tool_calls"),
            ],
            [ProviderTextDelta("第二份计划"), ProviderTurnEnd("end_turn")],
        ]
    )
    loop = AgentLoop(provider, make_registry(write=write))

    first_events = await collect_with_plan_decisions(
        loop,
        ConversationHistory(),
        AgentRunContext(),
        [("execute_current", "")],
    )
    second_events: list[AgentEvent] = []
    second_context = AgentRunContext()
    async for event in loop.run(
        "第二次规划",
        ConversationHistory(),
        plan_only=True,
        context=second_context,
    ):
        second_events.append(event)
        if isinstance(event, ToolApprovalRequestedEvent):
            second_context.resolve_tool_approval(event.request_id, "reject")
        elif isinstance(event, PlanApprovalRequestedEvent):
            second_context.resolve_plan_approval(event.request_id, "reject")

    assert first_events[-1] == FinalResponseEvent("第一次完成", 3)
    approvals = [
        event
        for event in second_events
        if isinstance(event, ToolApprovalRequestedEvent)
    ]
    assert [event.call_id for event in approvals] == ["write_2"]
    assert write.executions == 1
    assert second_events[-1] == RunCancelledEvent("plan_rejected")


@pytest.mark.asyncio
async def test_plan_changes_add_feedback_as_user_event_and_history() -> None:
    provider = ScriptedProvider(
        [
            [ProviderTextDelta("初版计划"), ProviderTurnEnd("end_turn")],
            [ProviderTextDelta("新版计划"), ProviderTurnEnd("end_turn")],
        ]
    )
    history = ConversationHistory()

    events = await collect_with_plan_decisions(
        AgentLoop(provider, make_registry()),
        history,
        AgentRunContext(),
        [("request_changes", "补充回滚步骤"), ("reject", "")],
    )

    assert UserMessageEvent("补充回滚步骤") in events
    assert ChatMessage(role="user", content="补充回滚步骤") in history.snapshot()
    assert events[-1] == RunCancelledEvent("plan_rejected")


@pytest.mark.asyncio
async def test_plan_on_final_round_disables_execution_and_changes() -> None:
    provider = ScriptedProvider(
        [[ProviderTextDelta("最终计划"), ProviderTurnEnd("end_turn")]]
    )
    events = await collect_with_plan_decisions(
        AgentLoop(
            provider,
            make_registry(),
            config=AgentLoopConfig(max_rounds=1),
        ),
        ConversationHistory(),
        AgentRunContext(),
        [("reject", "")],
    )

    approval = next(
        event
        for event in events
        if isinstance(event, PlanApprovalRequestedEvent)
    )
    assert approval.can_execute is False
    assert approval.can_request_changes is False
    assert events[-1] == RunCancelledEvent("round_limit_after_plan")


@pytest.mark.asyncio
async def test_cancelled_tool_batch_completes_every_tool_call_id() -> None:
    read = EchoReadTool()
    write = RecordingWriteTool()
    provider = ScriptedProvider(
        [
            [
                ProviderToolCall(
                    ToolCall("read_1", "echo_read", '{"value":1}')
                ),
                ProviderToolCall(
                    ToolCall("write_1", "record_write", "{}")
                ),
                ProviderTurnEnd("tool_calls"),
            ]
        ]
    )
    history = ConversationHistory()
    context = AgentRunContext()
    events: list[AgentEvent] = []

    async for event in AgentLoop(
        provider,
        make_registry(read=read, write=write),
    ).run(
        "任务",
        history,
        plan_only=False,
        context=context,
    ):
        events.append(event)
        if (
            isinstance(event, ToolCallStartedEvent)
            and event.call_id == "read_1"
        ):
            context.cancel()

    tool_messages = [
        message for message in history.snapshot() if message.role == "tool"
    ]
    assert [message.tool_call_id for message in tool_messages] == [
        "read_1",
        "write_1",
    ]
    assert json.loads(tool_messages[0].content)["success"] is True
    assert json.loads(tool_messages[1].content)["error"]["code"] == (
        "tool_cancelled"
    )
    assert read.executions == 1
    assert write.executions == 0
    assert events[-1] == RunCancelledEvent("user_cancelled")
    assert len(provider.requests) == 1
