from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from mewcode_agent.config import ProviderConfig
from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall
from mewcode_agent.providers.anthropic_provider import AnthropicProvider
from mewcode_agent.providers.base import (
    ProviderError,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderThinkingComplete,
    ProviderThinkingDelta,
    ProviderToolCall,
    ProviderTurnEnd,
)


class FakeAnthropicEventStream:
    def __init__(
        self,
        events: list[Any],
        error: Exception | None = None,
    ) -> None:
        self._events = events
        self._error = error

    async def __aiter__(self) -> AsyncIterator[Any]:
        for event in self._events:
            yield event
        if self._error is not None:
            raise self._error


class FakeAnthropicEventManager:
    def __init__(
        self,
        events: list[Any],
        error: Exception | None = None,
    ) -> None:
        self._stream = FakeAnthropicEventStream(events, error)

    async def __aenter__(self) -> FakeAnthropicEventStream:
        return self._stream

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeAnthropicStream:
    def __init__(
        self,
        manager: FakeAnthropicEventManager,
        error: Exception | None = None,
    ) -> None:
        self.manager = manager
        self.error = error
        self.kwargs: dict[str, Any] | None = None

    def __call__(self, **kwargs: Any) -> FakeAnthropicEventManager:
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.manager


def make_client(stream: FakeAnthropicStream) -> Any:
    return SimpleNamespace(messages=SimpleNamespace(stream=stream))


def text_delta(text: str) -> Any:
    return SimpleNamespace(
        type="content_block_delta",
        index=0,
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def message_delta(stop_reason: str | None) -> Any:
    return SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason=stop_reason),
    )


async def collect(provider: AnthropicProvider) -> list[ProviderStreamEvent]:
    return [
        event
        async for event in provider.stream_chat(
            [ChatMessage(role="user", content="你好")],
            system_prompt="system text",
        )
    ]


@pytest.mark.asyncio
async def test_anthropic_provider_streams_text_and_request_shape(
    anthropic_config: ProviderConfig,
) -> None:
    stream = FakeAnthropicStream(
        FakeAnthropicEventManager(
            [text_delta("你"), text_delta("好"), message_delta("end_turn")]
        )
    )
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    events = await collect(provider)

    assert events == [
        ProviderTextDelta("你"),
        ProviderTextDelta("好"),
        ProviderTurnEnd("end_turn"),
    ]
    assert stream.kwargs == {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 4096,
        "system": "system text",
    }


@pytest.mark.asyncio
async def test_anthropic_provider_maps_thinking_signature_and_stop_reason(
    anthropic_config: ProviderConfig,
) -> None:
    events = [
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="thinking",
                thinking="",
                signature="",
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(
                type="thinking_delta",
                thinking="先分析",
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(
                type="signature_delta",
                signature="sig-1",
            ),
        ),
        SimpleNamespace(type="content_block_stop", index=0),
        text_delta("答案"),
        message_delta("end_turn"),
    ]
    stream = FakeAnthropicStream(FakeAnthropicEventManager(events))
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    result = await collect(provider)

    assert result == [
        ProviderThinkingDelta("先分析"),
        ProviderThinkingComplete(ThinkingBlock("先分析", "sig-1")),
        ProviderTextDelta("答案"),
        ProviderTurnEnd("end_turn"),
    ]


@pytest.mark.asyncio
async def test_anthropic_provider_assembles_streamed_tool_call(
    anthropic_config: ProviderConfig,
) -> None:
    events = [
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="tool_use",
                id="toolu_1",
                name="read_file",
                input={},
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"pa'),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(
                type="input_json_delta",
                partial_json='th":"README.md"}',
            ),
        ),
        message_delta("tool_use"),
    ]
    stream = FakeAnthropicStream(FakeAnthropicEventManager(events))
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )
    tools = [{"name": "read_file", "input_schema": {"type": "object"}}]

    result = [
        event
        async for event in provider.stream_chat(
            [ChatMessage(role="user", content="读取 README")],
            tools=tools,
            system_prompt="system text",
        )
    ]

    assert result == [
        ProviderToolCall(
            ToolCall(
                call_id="toolu_1",
                name="read_file",
                arguments_json='{"path":"README.md"}',
            )
        ),
        ProviderTurnEnd("tool_calls"),
    ]
    assert stream.kwargs == {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "读取 README"}],
        "max_tokens": 4096,
        "system": "system text",
        "tools": tools,
        "tool_choice": {"type": "auto"},
    }


@pytest.mark.asyncio
async def test_anthropic_provider_returns_multiple_tool_calls_in_index_order(
    anthropic_config: ProviderConfig,
) -> None:
    events = [
        SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(
                type="tool_use",
                id="toolu_2",
                name="read_file",
                input={"path": "two"},
            ),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="tool_use",
                id="toolu_1",
                name="read_file",
                input={"path": "one"},
            ),
        ),
        message_delta("tool_use"),
    ]
    stream = FakeAnthropicStream(FakeAnthropicEventManager(events))
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    result = [
        event
        async for event in provider.stream_chat(
            [ChatMessage(role="user", content="读取两个文件")],
            tools=[{"name": "read_file", "input_schema": {"type": "object"}}],
            system_prompt="system text",
        )
    ]

    assert result == [
        ProviderToolCall(ToolCall("toolu_1", "read_file", '{"path":"one"}')),
        ProviderToolCall(ToolCall("toolu_2", "read_file", '{"path":"two"}')),
        ProviderTurnEnd("tool_calls"),
    ]


def test_anthropic_provider_serializes_thinking_before_tool_use() -> None:
    first_call = ToolCall("toolu_1", "read_file", '{"path":"one"}')
    second_call = ToolCall("toolu_2", "read_file", '{"path":"two"}')

    request = AnthropicProvider._request_messages(
        [
            ChatMessage(
                role="assistant",
                content="说明",
                tool_calls=(first_call, second_call),
                thinking_blocks=(ThinkingBlock("先分析", "sig-1"),),
            ),
            ChatMessage(
                role="tool",
                content='{"success":true}',
                tool_call_id="toolu_1",
            ),
            ChatMessage(
                role="tool",
                content='{"success":false}',
                tool_call_id="toolu_2",
            ),
        ]
    )

    assert request == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "先分析",
                    "signature": "sig-1",
                },
                {"type": "text", "text": "说明"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": "one"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_2",
                    "name": "read_file",
                    "input": {"path": "two"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": '{"success":true}',
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_2",
                    "content": '{"success":false}',
                },
            ],
        },
    ]


@pytest.mark.asyncio
async def test_anthropic_provider_leaves_empty_response_for_agent_validation(
    anthropic_config: ProviderConfig,
) -> None:
    stream = FakeAnthropicStream(FakeAnthropicEventManager([]))
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    assert await collect(provider) == [ProviderTurnEnd("other")]


@pytest.mark.asyncio
async def test_anthropic_provider_preserves_whitespace_text_delta(
    anthropic_config: ProviderConfig,
) -> None:
    stream = FakeAnthropicStream(
        FakeAnthropicEventManager(
            [text_delta(" \n"), message_delta("end_turn")]
        )
    )
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    assert await collect(provider) == [
        ProviderTextDelta(" \n"),
        ProviderTurnEnd("end_turn"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_reason", "expected"),
    [
        ("end_turn", "end_turn"),
        ("tool_use", "tool_calls"),
        ("max_tokens", "max_tokens"),
        ("stop_sequence", "other"),
        (None, "other"),
    ],
)
async def test_anthropic_provider_maps_stop_reason(
    anthropic_config: ProviderConfig,
    raw_reason: str | None,
    expected: str,
) -> None:
    stream = FakeAnthropicStream(
        FakeAnthropicEventManager([text_delta("x"), message_delta(raw_reason)])
    )
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    result = await collect(provider)

    assert result[-1] == ProviderTurnEnd(expected)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_anthropic_provider_rejects_tool_delta_without_start(
    anthropic_config: ProviderConfig,
) -> None:
    event = SimpleNamespace(
        type="content_block_delta",
        index=0,
        delta=SimpleNamespace(type="input_json_delta", partial_json="{}"),
    )
    stream = FakeAnthropicStream(FakeAnthropicEventManager([event]))
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    with pytest.raises(ProviderError, match="无起始块的工具参数"):
        await collect(provider)


@pytest.mark.asyncio
async def test_anthropic_provider_sanitizes_stream_failure(
    anthropic_config: ProviderConfig,
) -> None:
    stream = FakeAnthropicStream(
        FakeAnthropicEventManager(
            [],
            RuntimeError("test-secret must not leak"),
        )
    )
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    with pytest.raises(ProviderError, match="流式响应中断") as caught:
        await collect(provider)

    assert "test-secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_kind", "expected_message"),
    [
        ("authentication", "鉴权失败"),
        ("rate_limit", "触发限流"),
        ("timeout", "请求超时"),
        ("connection", "无法连接"),
        ("status", "HTTP 500"),
        ("api", "请求失败"),
    ],
)
async def test_anthropic_provider_maps_sdk_errors(
    anthropic_config: ProviderConfig,
    error_kind: str,
    expected_message: str,
) -> None:
    import anthropic
    import httpx

    request = httpx.Request(
        "POST",
        "https://api.deepseek.com/anthropic/v1/messages",
    )
    response = httpx.Response(500, request=request)
    errors = {
        "authentication": anthropic.AuthenticationError(
            "secret raw error",
            response=httpx.Response(401, request=request),
            body=None,
        ),
        "rate_limit": anthropic.RateLimitError(
            "secret raw error",
            response=httpx.Response(429, request=request),
            body=None,
        ),
        "timeout": anthropic.APITimeoutError(request=request),
        "connection": anthropic.APIConnectionError(
            message="secret raw error",
            request=request,
        ),
        "status": anthropic.APIStatusError(
            "secret raw error",
            response=response,
            body=None,
        ),
        "api": anthropic.APIError("secret raw error", request, body=None),
    }
    stream = FakeAnthropicStream(
        FakeAnthropicEventManager([]),
        errors[error_kind],
    )
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    with pytest.raises(ProviderError, match=expected_message) as caught:
        await collect(provider)

    assert "secret raw error" not in str(caught.value)
