from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from mewcode_agent.config import ProviderConfig
from mewcode_agent.models import ChatMessage, ToolCall
from mewcode_agent.providers.anthropic_provider import AnthropicProvider
from mewcode_agent.providers.base import ProviderError


class FakeTextStream:
    def __init__(self, parts: list[str], error: Exception | None = None) -> None:
        self._parts = parts
        self._error = error

    async def __aiter__(self) -> AsyncIterator[str]:
        for part in self._parts:
            yield part
        if self._error is not None:
            raise self._error


class FakeAnthropicManager:
    def __init__(self, text_stream: FakeTextStream) -> None:
        self._stream = SimpleNamespace(text_stream=text_stream)

    async def __aenter__(self) -> Any:
        return self._stream

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeAnthropicEventStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def __aiter__(self) -> AsyncIterator[Any]:
        for event in self._events:
            yield event


class FakeAnthropicEventManager:
    def __init__(self, events: list[Any]) -> None:
        self._stream = FakeAnthropicEventStream(events)

    async def __aenter__(self) -> FakeAnthropicEventStream:
        return self._stream

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeAnthropicStream:
    def __init__(
        self,
        manager: FakeAnthropicManager,
        error: Exception | None = None,
    ) -> None:
        self.manager = manager
        self.error = error
        self.kwargs: dict[str, Any] | None = None

    def __call__(self, **kwargs: Any) -> FakeAnthropicManager:
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.manager


def make_client(stream: FakeAnthropicStream) -> Any:
    return SimpleNamespace(messages=SimpleNamespace(stream=stream))


async def collect(provider: AnthropicProvider) -> list[str]:
    return [
        part
        async for part in provider.stream_chat(
            [ChatMessage(role="user", content="你好")]
        )
    ]


@pytest.mark.asyncio
async def test_anthropic_provider_streams_text_and_request_shape(
    anthropic_config: ProviderConfig,
) -> None:
    stream = FakeAnthropicStream(
        FakeAnthropicManager(FakeTextStream(["你", "", "好"]))
    )
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    parts = await collect(provider)

    assert parts == ["你", "好"]
    assert stream.kwargs == {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 4096,
    }


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
    ]
    stream = FakeAnthropicStream(FakeAnthropicEventManager(events))  # type: ignore[arg-type]
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )
    tools = [{"name": "read_file", "input_schema": {"type": "object"}}]

    parts = [
        part
        async for part in provider.stream_chat(
            [ChatMessage(role="user", content="读取 README")],
            tools=tools,
        )
    ]

    assert parts == [
        ToolCall(
            call_id="toolu_1",
            name="read_file",
            arguments_json='{"path":"README.md"}',
        )
    ]
    assert stream.kwargs == {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "读取 README"}],
        "max_tokens": 4096,
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
    ]
    stream = FakeAnthropicStream(FakeAnthropicEventManager(events))  # type: ignore[arg-type]
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    parts = [
        part
        async for part in provider.stream_chat(
            [ChatMessage(role="user", content="读取两个文件")],
            tools=[{"name": "read_file", "input_schema": {"type": "object"}}],
        )
    ]

    assert parts == [
        ToolCall("toolu_1", "read_file", '{"path":"one"}'),
        ToolCall("toolu_2", "read_file", '{"path":"two"}'),
    ]


def test_anthropic_provider_serializes_tool_history() -> None:
    first_call = ToolCall("toolu_1", "read_file", '{"path":"one"}')
    second_call = ToolCall("toolu_2", "read_file", '{"path":"two"}')

    request = AnthropicProvider._request_messages(
        [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=(first_call, second_call),
            ),
            ChatMessage(role="tool", content='{"success":true}', tool_call_id="toolu_1"),
            ChatMessage(role="tool", content='{"success":false}', tool_call_id="toolu_2"),
        ]
    )

    assert request == [
        {
            "role": "assistant",
            "content": [
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
async def test_anthropic_provider_rejects_empty_response(
    anthropic_config: ProviderConfig,
) -> None:
    stream = FakeAnthropicStream(FakeAnthropicManager(FakeTextStream([""])))
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    with pytest.raises(ProviderError, match="空响应"):
        await collect(provider)


@pytest.mark.asyncio
async def test_anthropic_provider_rejects_whitespace_only_response(
    anthropic_config: ProviderConfig,
) -> None:
    stream = FakeAnthropicStream(FakeAnthropicManager(FakeTextStream([" \n"])))
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    with pytest.raises(ProviderError, match="空响应"):
        await collect(provider)


@pytest.mark.asyncio
async def test_anthropic_provider_sanitizes_stream_failure(
    anthropic_config: ProviderConfig,
) -> None:
    stream = FakeAnthropicStream(
        FakeAnthropicManager(
            FakeTextStream([], RuntimeError("test-secret must not leak"))
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

    request = httpx.Request("POST", "https://api.deepseek.com/anthropic/v1/messages")
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
        FakeAnthropicManager(FakeTextStream([])),
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
