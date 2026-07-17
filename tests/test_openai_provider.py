from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from mewcode_agent.config import ProviderConfig
from mewcode_agent.models import ChatMessage, ToolCall
from mewcode_agent.providers.base import ProviderError
from mewcode_agent.providers.openai_provider import OpenAIProvider


class FakeOpenAIStream:
    def __init__(self, chunks: list[Any], error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error

    async def __aiter__(self) -> AsyncIterator[Any]:
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


class FakeOpenAICreate:
    def __init__(
        self,
        stream: FakeOpenAIStream,
        error: Exception | None = None,
    ) -> None:
        self.stream = stream
        self.error = error
        self.kwargs: dict[str, Any] | None = None

    async def __call__(self, **kwargs: Any) -> FakeOpenAIStream:
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.stream


def make_chunk(text: str | None, *, with_choices: bool = True) -> Any:
    choices = []
    if with_choices:
        choices.append(SimpleNamespace(delta=SimpleNamespace(content=text)))
    return SimpleNamespace(choices=choices)


def make_tool_chunk(
    *,
    index: int,
    call_id: str | None,
    name: str | None,
    arguments: str | None,
) -> Any:
    delta = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                index=index,
                id=call_id,
                function=SimpleNamespace(name=name, arguments=arguments),
            )
        ],
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def make_client(create: FakeOpenAICreate) -> Any:
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        )
    )


async def collect(provider: OpenAIProvider) -> list[str]:
    return [
        part
        async for part in provider.stream_chat(
            [ChatMessage(role="user", content="你好")]
        )
    ]


@pytest.mark.asyncio
async def test_openai_provider_streams_text_and_request_shape(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream(
            [make_chunk(None), make_chunk("你"), make_chunk("好"), make_chunk("", with_choices=False)]
        )
    )
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    parts = await collect(provider)

    assert parts == ["你", "好"]
    assert create.kwargs == {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 4096,
        "stream": True,
    }


@pytest.mark.asyncio
async def test_openai_provider_assembles_streamed_tool_call(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream(
            [
                make_tool_chunk(
                    index=0,
                    call_id="call_1",
                    name="read_",
                    arguments='{"pa',
                ),
                make_tool_chunk(
                    index=0,
                    call_id=None,
                    name="file",
                    arguments='th":"README.md"}',
                ),
            ]
        )
    )
    provider = OpenAIProvider(openai_config, "test-secret", client=make_client(create))
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    parts = [
        part
        async for part in provider.stream_chat(
            [ChatMessage(role="user", content="读取 README")],
            tools=tools,
        )
    ]

    assert parts == [
        ToolCall(
            call_id="call_1",
            name="read_file",
            arguments_json='{"path":"README.md"}',
        )
    ]
    assert create.kwargs == {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "读取 README"}],
        "max_tokens": 4096,
        "stream": True,
        "tools": tools,
    }


@pytest.mark.asyncio
async def test_openai_provider_returns_multiple_tool_calls_in_index_order(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream(
            [
                make_tool_chunk(
                    index=1,
                    call_id="call_2",
                    name="read_file",
                    arguments='{"path":"two"}',
                ),
                make_tool_chunk(
                    index=0,
                    call_id="call_1",
                    name="read_file",
                    arguments='{"path":"one"}',
                ),
            ]
        )
    )
    provider = OpenAIProvider(openai_config, "test-secret", client=make_client(create))

    parts = [
        part
        async for part in provider.stream_chat(
            [ChatMessage(role="user", content="读取两个文件")],
            tools=[{"type": "function", "function": {"name": "read_file"}}],
        )
    ]

    assert parts == [
        ToolCall("call_1", "read_file", '{"path":"one"}'),
        ToolCall("call_2", "read_file", '{"path":"two"}'),
    ]


def test_openai_provider_serializes_tool_history() -> None:
    call = ToolCall("call_1", "read_file", '{"path":"README.md"}')

    request = OpenAIProvider._request_messages(
        [
            ChatMessage(role="assistant", content="", tool_calls=(call,)),
            ChatMessage(role="tool", content='{"success":true}', tool_call_id="call_1"),
        ]
    )

    assert request == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"README.md"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"success":true}'},
    ]


@pytest.mark.asyncio
async def test_openai_provider_rejects_empty_response(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(FakeOpenAIStream([make_chunk(None)]))
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    with pytest.raises(ProviderError, match="空响应"):
        await collect(provider)


@pytest.mark.asyncio
async def test_openai_provider_rejects_whitespace_only_response(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(FakeOpenAIStream([make_chunk("  \n")]))
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    with pytest.raises(ProviderError, match="空响应"):
        await collect(provider)


@pytest.mark.asyncio
async def test_openai_provider_sanitizes_stream_failure(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream([], RuntimeError("test-secret must not leak"))
    )
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
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
async def test_openai_provider_maps_sdk_errors(
    openai_config: ProviderConfig,
    error_kind: str,
    expected_message: str,
) -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(500, request=request)
    errors = {
        "authentication": openai.AuthenticationError(
            "secret raw error",
            response=httpx.Response(401, request=request),
            body=None,
        ),
        "rate_limit": openai.RateLimitError(
            "secret raw error",
            response=httpx.Response(429, request=request),
            body=None,
        ),
        "timeout": openai.APITimeoutError(request=request),
        "connection": openai.APIConnectionError(
            message="secret raw error",
            request=request,
        ),
        "status": openai.APIStatusError(
            "secret raw error",
            response=response,
            body=None,
        ),
        "api": openai.APIError("secret raw error", request, body=None),
    }
    create = FakeOpenAICreate(FakeOpenAIStream([]), errors[error_kind])
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    with pytest.raises(ProviderError, match=expected_message) as caught:
        await collect(provider)

    assert "secret raw error" not in str(caught.value)
