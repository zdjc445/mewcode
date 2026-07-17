from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from textual.widgets import Input, RichLog, Static

from mewcode_agent.app import ChatApp, MAX_TOOL_CALLS_PER_TURN
from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage, ToolCall
from mewcode_agent.providers.base import ProviderError
from mewcode_agent.tools import Tool, ToolRegistry


class GatedProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.received_messages: list[ChatMessage] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
    ) -> AsyncIterator[str]:
        self.received_messages = messages
        yield "分片"
        self.started.set()
        await self.release.wait()
        yield "完成"


class ErrorProvider:
    async def stream_chat(
        self,
        messages: list[ChatMessage],
    ) -> AsyncIterator[str]:
        if False:
            yield ""
        raise ProviderError("模拟失败")


class RecordingProvider:
    def __init__(self) -> None:
        self.requests: list[list[ChatMessage]] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
    ) -> AsyncIterator[str]:
        self.requests.append(messages)
        yield f"回答{len(self.requests)}"


class CountingTool(Tool):
    name = "count"
    description = "测试工具"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
    }

    def __init__(self) -> None:
        self.values: list[int] = []

    async def execute(self, arguments: dict[str, object]) -> dict[str, object]:
        value = arguments["value"]
        assert isinstance(value, int)
        self.values.append(value)
        return {"value": value}


class ToolCallingProvider:
    protocol = "openai"

    def __init__(self) -> None:
        self.requests: list[list[ChatMessage]] = []
        self.tools: list[list[dict[str, object]]] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[str | ToolCall]:
        self.requests.append(messages)
        self.tools.append(tools or [])
        turn_number = sum(message.role == "user" for message in messages)
        if messages[-1].role == "user":
            yield ToolCall(
                f"call_{turn_number}",
                "count",
                f'{{"value":{turn_number}}}',
            )
        else:
            yield f"完成{turn_number}"


class MultiToolProvider:
    protocol = "openai"

    def __init__(self) -> None:
        self.requests: list[list[ChatMessage]] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[str | ToolCall]:
        self.requests.append(messages)
        round_number = len(self.requests)
        if round_number == 1:
            yield ToolCall("call_1", "count", '{"value":1}')
            yield ToolCall("call_2", "count", '{"value":2}')
        elif round_number == 2:
            yield ToolCall("call_3", "count", '{"value":3}')
        else:
            yield "全部完成"


class EndlessToolProvider:
    protocol = "openai"

    def __init__(self) -> None:
        self.received_tools: list[list[dict[str, object]] | None] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[str | ToolCall]:
        self.received_tools.append(tools)
        if tools is None:
            yield "已根据现有结果总结"
        else:
            number = len(self.received_tools)
            yield ToolCall(
                f"call_{number}",
                "count",
                f'{{"value":{number}}}',
            )


class OversizedBatchProvider:
    protocol = "openai"

    def __init__(self) -> None:
        self.request_count = 0

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[str | ToolCall]:
        self.request_count += 1
        if tools is None:
            yield "批量调用处理完成"
            return
        for number in range(1, MAX_TOOL_CALLS_PER_TURN + 2):
            yield ToolCall(
                f"batch_{number}",
                "count",
                f'{{"value":{number}}}',
            )


def render_log_text(log: RichLog) -> str:
    return "\n".join(strip.text for strip in log.lines)


@pytest.mark.asyncio
async def test_app_streams_and_restores_input() -> None:
    provider = GatedProvider()
    history = ConversationHistory()
    app = ChatApp(
        provider,
        history,
        provider_id="deepseek_openai",
        model="deepseek-v4-pro",
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "记住 42"
        await pilot.press("enter")
        await provider.started.wait()
        await pilot.pause()

        assert prompt_input.disabled is True
        assert app.active_response == "分片"
        assert "Assistant: 分片" in render_log_text(
            app.query_one("#chat-log", RichLog)
        )

        provider.release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert prompt_input.disabled is False
        assert prompt_input.has_focus is True
        assert history.snapshot() == [
            ChatMessage(role="user", content="记住 42"),
            ChatMessage(role="assistant", content="分片完成"),
        ]
        assert provider.received_messages == [
            ChatMessage(role="user", content="记住 42")
        ]


@pytest.mark.asyncio
async def test_app_ignores_blank_input() -> None:
    provider = GatedProvider()
    history = ConversationHistory()
    app = ChatApp(
        provider,
        history,
        provider_id="deepseek_openai",
        model="deepseek-v4-pro",
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "   "
        await pilot.press("enter")
        await pilot.pause()

        assert len(history) == 0
        assert prompt_input.disabled is False
        assert not provider.started.is_set()


@pytest.mark.asyncio
async def test_app_recovers_from_provider_error_without_adding_error_to_history() -> None:
    history = ConversationHistory()
    app = ChatApp(
        ErrorProvider(),
        history,
        provider_id="deepseek_openai",
        model="deepseek-v4-pro",
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "触发错误"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        status = app.query_one("#status", Static)
        assert "错误：模拟失败" in str(status.render())
        assert prompt_input.disabled is False
        assert prompt_input.has_focus is True
        assert history.snapshot() == [
            ChatMessage(role="user", content="触发错误")
        ]


@pytest.mark.asyncio
async def test_second_turn_sends_complete_first_turn_history() -> None:
    provider = RecordingProvider()
    history = ConversationHistory()
    app = ChatApp(
        provider,
        history,
        provider_id="deepseek_openai",
        model="deepseek-v4-pro",
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "第一问"
        await pilot.press("enter")
        await app.workers.wait_for_complete()

        prompt_input.value = "第二问"
        await pilot.press("enter")
        await app.workers.wait_for_complete()

        assert provider.requests[1] == [
            ChatMessage(role="user", content="第一问"),
            ChatMessage(role="assistant", content="回答1"),
            ChatMessage(role="user", content="第二问"),
        ]


@pytest.mark.asyncio
async def test_each_user_request_can_execute_one_tool() -> None:
    provider = ToolCallingProvider()
    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    history = ConversationHistory()
    app = ChatApp(
        provider,
        history,
        provider_id="deepseek_openai",
        model="deepseek-v4-pro",
        tool_registry=registry,
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "第一次"
        await pilot.press("enter")
        await app.workers.wait_for_complete()

        prompt_input.value = "第二次"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert tool.values == [1, 2]
    assert len(provider.requests) == 4
    assert len(provider.tools[0]) == 1
    assert provider.requests[1] == history.snapshot()[:3]
    assert provider.requests[2] == history.snapshot()[:5]
    assert history.snapshot()[1].tool_calls == (
        ToolCall("call_1", "count", '{"value":1}'),
    )
    first_result = history.snapshot()[2]
    assert first_result.role == "tool"
    assert first_result.tool_call_id == "call_1"
    assert '"success":true' in first_result.content
    assert history.snapshot()[-1] == ChatMessage(
        role="assistant",
        content="完成2",
    )
    assert prompt_input.disabled is False


@pytest.mark.asyncio
async def test_agent_loop_executes_parallel_calls_in_order_and_continues() -> None:
    provider = MultiToolProvider()
    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    history = ConversationHistory()
    app = ChatApp(
        provider,
        history,
        provider_id="deepseek_openai",
        model="deepseek-v4-pro",
        tool_registry=registry,
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "执行多个工具"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert tool.values == [1, 2, 3]
    assert len(provider.requests) == 3
    assert history.snapshot()[1].tool_calls == (
        ToolCall("call_1", "count", '{"value":1}'),
        ToolCall("call_2", "count", '{"value":2}'),
    )
    assert history.snapshot()[-1] == ChatMessage(
        role="assistant",
        content="全部完成",
    )
    assert prompt_input.disabled is False


@pytest.mark.asyncio
async def test_agent_loop_disables_tools_for_summary_after_limit() -> None:
    provider = EndlessToolProvider()
    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    history = ConversationHistory()
    app = ChatApp(
        provider,
        history,
        provider_id="deepseek_openai",
        model="deepseek-v4-pro",
        tool_registry=registry,
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "持续调用工具"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert tool.values == list(range(1, MAX_TOOL_CALLS_PER_TURN + 1))
    assert len(provider.received_tools) == MAX_TOOL_CALLS_PER_TURN + 1
    assert all(
        tools is not None
        for tools in provider.received_tools[:MAX_TOOL_CALLS_PER_TURN]
    )
    assert provider.received_tools[-1] is None
    assert history.snapshot()[-1] == ChatMessage(
        role="assistant",
        content="已根据现有结果总结",
    )
    assert prompt_input.disabled is False


@pytest.mark.asyncio
async def test_agent_loop_skips_batch_calls_beyond_limit() -> None:
    provider = OversizedBatchProvider()
    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    history = ConversationHistory()
    app = ChatApp(
        provider,
        history,
        provider_id="deepseek_openai",
        model="deepseek-v4-pro",
        tool_registry=registry,
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "一次请求超过工具上限"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert tool.values == list(range(1, MAX_TOOL_CALLS_PER_TURN + 1))
    skipped_result = next(
        message
        for message in history.snapshot()
        if message.tool_call_id == f"batch_{MAX_TOOL_CALLS_PER_TURN + 1}"
    )
    assert '"code":"tool_limit_reached"' in skipped_result.content
    assert provider.request_count == 2
    assert history.snapshot()[-1].content == "批量调用处理完成"
