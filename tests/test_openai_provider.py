from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from mewcode_agent.config import ProviderConfig
from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall
from mewcode_agent.prompting.models import (
    ControlMessage,
    ContextBoundaryMessage,
    ContextSummaryMessage,
)
from mewcode_agent.providers.base import (
    ProviderError,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderThinkingComplete,
    ProviderThinkingDelta,
    ProviderToolCall,
    ProviderTurnEnd,
    ProviderUsage,
    ProviderUsageEvent,
    ProviderUsageResult,
)
from mewcode_agent.providers.openai_provider import OpenAIProvider


def test_openai_serializes_context_summary_and_boundary_as_system() -> None:
    request = ProviderRequest(
        "system",
        (
            ContextSummaryMessage(1, 2, '{"schema_version":1}'),
            ContextBoundaryMessage(1, "重新读取精确内容"),
        ),
        None,
    )

    messages = OpenAIProvider._request_messages(request)

    assert [message["role"] for message in messages] == [
        "system",
        "system",
        "system",
    ]
    assert "<mewcode-summary" in messages[1]["content"]
    assert "&quot;" not in messages[1]["content"]
    assert "<mewcode-boundary" in messages[2]["content"]


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


def make_chunk(
    text: str | None,
    *,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
    with_choices: bool = True,
    usage: Any | None = None,
) -> Any:
    choices: list[Any] = []
    if with_choices:
        delta = SimpleNamespace(
            content=text,
            reasoning_content=reasoning_content,
            tool_calls=None,
        )
        choices.append(
            SimpleNamespace(delta=delta, finish_reason=finish_reason)
        )
    return SimpleNamespace(choices=choices, usage=usage)


def make_tool_chunk(
    *,
    index: int,
    call_id: str | None,
    name: str | None,
    arguments: str | None,
    finish_reason: str | None = None,
) -> Any:
    delta = SimpleNamespace(
        content=None,
        reasoning_content=None,
        tool_calls=[
            SimpleNamespace(
                index=index,
                id=call_id,
                function=SimpleNamespace(name=name, arguments=arguments),
            )
        ],
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def make_client(create: FakeOpenAICreate) -> Any:
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        )
    )


async def collect(provider: OpenAIProvider) -> list[ProviderStreamEvent]:
    return [
        event
        async for event in provider.stream_chat(
            request_for(ChatMessage(role="user", content="你好"))
        )
    ]


def request_for(*items: ChatMessage | ControlMessage) -> ProviderRequest:
    return ProviderRequest("system text", tuple(items), None)


OPENAI_USAGE_MISSING_EVENT = ProviderUsageEvent(
    ProviderUsageResult("unavailable", None, "openai_usage_missing")
)


def openai_usage(**overrides: object) -> Any:
    fields: dict[str, object] = {
        "prompt_tokens": 150,
        "prompt_cache_hit_tokens": 120,
        "prompt_cache_miss_tokens": 30,
        "completion_tokens": 9,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
async def test_openai_provider_streams_text_and_request_shape(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream(
            [
                make_chunk(None),
                make_chunk("你"),
                make_chunk("好", finish_reason="stop"),
                make_chunk("", with_choices=False),
            ]
        )
    )
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    events = await collect(provider)

    assert events == [
        ProviderTextDelta("你"),
        ProviderTextDelta("好"),
        OPENAI_USAGE_MISSING_EVENT,
        ProviderTurnEnd("end_turn"),
    ]
    assert create.kwargs == {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "system text"},
            {"role": "user", "content": "你好"},
        ],
        "max_tokens": 4096,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


@pytest.mark.asyncio
async def test_openai_provider_maps_thinking_text_and_stop_reason(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream(
            [
                make_chunk(None, reasoning_content="先分析"),
                make_chunk("答案", finish_reason="stop"),
            ]
        )
    )
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    events = await collect(provider)

    assert events == [
        ProviderThinkingDelta("先分析"),
        ProviderTextDelta("答案"),
        ProviderThinkingComplete(ThinkingBlock("先分析")),
        OPENAI_USAGE_MISSING_EVENT,
        ProviderTurnEnd("end_turn"),
    ]


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
                    finish_reason="tool_calls",
                ),
            ]
        )
    )
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    events = [
        event
        async for event in provider.stream_chat(
            ProviderRequest(
                "system text",
                (ChatMessage(role="user", content="读取 README"),),
                tuple(tools),
            )
        )
    ]

    assert events == [
        ProviderToolCall(
            ToolCall(
                call_id="call_1",
                name="read_file",
                arguments_json='{"path":"README.md"}',
            )
        ),
        OPENAI_USAGE_MISSING_EVENT,
        ProviderTurnEnd("tool_calls"),
    ]
    assert create.kwargs == {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "system text"},
            {"role": "user", "content": "读取 README"},
        ],
        "max_tokens": 4096,
        "stream": True,
        "stream_options": {"include_usage": True},
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
                    finish_reason="tool_calls",
                ),
            ]
        )
    )
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    events = [
        event
        async for event in provider.stream_chat(
            ProviderRequest(
                "system text",
                (ChatMessage(role="user", content="读取两个文件"),),
                (
                    {
                        "type": "function",
                        "function": {"name": "read_file"},
                    },
                ),
            )
        )
    ]

    assert events == [
        ProviderToolCall(ToolCall("call_1", "read_file", '{"path":"one"}')),
        ProviderToolCall(ToolCall("call_2", "read_file", '{"path":"two"}')),
        OPENAI_USAGE_MISSING_EVENT,
        ProviderTurnEnd("tool_calls"),
    ]


def test_openai_provider_serializes_tool_history_with_reasoning() -> None:
    call = ToolCall("call_1", "read_file", '{"path":"README.md"}')

    request = OpenAIProvider._request_messages(
        ProviderRequest(
            "system text",
            (
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=(call,),
                    thinking_blocks=(
                        ThinkingBlock("先分析"),
                        ThinkingBlock("再调用"),
                    ),
                ),
                ChatMessage(
                    role="tool",
                    content='{"success":true}',
                    tool_call_id="call_1",
                ),
            ),
            None,
        )
    )

    assert request == [
        {"role": "system", "content": "system text"},
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
            "reasoning_content": "先分析再调用",
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"success":true}',
        },
    ]


def test_openai_request_keeps_stable_system_first_and_control_at_anchor() -> None:
    before = ControlMessage(
        "runtime.environment.session",
        "context",
        "session",
        '{"shell":"powershell.exe"}',
        1,
        0,
        None,
        None,
    )
    after = ControlMessage(
        "runtime.state.request_1.round_1",
        "state",
        "round",
        "当前运行状态",
        2,
        1,
        1,
        1,
    )

    messages = OpenAIProvider._request_messages(
        ProviderRequest(
            "stable system",
            (
                before,
                ChatMessage(role="user", content="任务"),
                after,
            ),
            None,
        )
    )

    assert [item["role"] for item in messages] == [
        "system",
        "system",
        "user",
        "system",
    ]
    assert messages[0] == {"role": "system", "content": "stable system"}
    assert messages[1]["content"].startswith("<mewcode-control\n")
    assert messages[2] == {"role": "user", "content": "任务"}
    assert 'kind="state"' in messages[3]["content"]


@pytest.mark.asyncio
async def test_openai_provider_emits_exact_available_usage_before_turn_end(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream(
            [
                make_chunk("OK", finish_reason="stop"),
                make_chunk(
                    None,
                    with_choices=False,
                    usage=openai_usage(),
                ),
            ]
        )
    )
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    events = await collect(provider)

    assert events[-2:] == [
        ProviderUsageEvent(
            ProviderUsageResult(
                "available",
                ProviderUsage(150, 120, 30, 9),
                None,
            )
        ),
        ProviderTurnEnd("end_turn"),
    ]
    assert create.kwargs["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize(
    ("raw", "status", "reason"),
    [
        (
            openai_usage(
                prompt_tokens=0,
                prompt_cache_hit_tokens=0,
                prompt_cache_miss_tokens=0,
                completion_tokens=0,
            ),
            "available",
            None,
        ),
        (
            openai_usage(prompt_cache_hit_tokens=None),
            "invalid",
            "openai_usage_fields_missing",
        ),
        (
            openai_usage(prompt_tokens=149),
            "invalid",
            "openai_usage_invalid",
        ),
    ],
)
def test_openai_usage_mapping(
    raw: Any,
    status: str,
    reason: str | None,
) -> None:
    result = OpenAIProvider._usage_result(raw)
    assert result.status == status
    assert result.reason == reason


@pytest.mark.asyncio
async def test_openai_provider_leaves_empty_response_for_agent_validation(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(FakeOpenAIStream([make_chunk(None)]))
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    assert await collect(provider) == [
        OPENAI_USAGE_MISSING_EVENT,
        ProviderTurnEnd("other"),
    ]


@pytest.mark.asyncio
async def test_openai_provider_preserves_whitespace_text_delta(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream([make_chunk("  \n", finish_reason="stop")])
    )
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    assert await collect(provider) == [
        ProviderTextDelta("  \n"),
        OPENAI_USAGE_MISSING_EVENT,
        ProviderTurnEnd("end_turn"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_reason", "expected"),
    [
        ("stop", "end_turn"),
        ("tool_calls", "tool_calls"),
        ("length", "max_tokens"),
        ("content_filter", "other"),
        (None, "other"),
    ],
)
async def test_openai_provider_maps_finish_reason(
    openai_config: ProviderConfig,
    raw_reason: str | None,
    expected: str,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream([make_chunk("x", finish_reason=raw_reason)])
    )
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    events = await collect(provider)

    assert events[-1] == ProviderTurnEnd(expected)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_openai_provider_rejects_incomplete_tool_call(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream(
            [
                make_tool_chunk(
                    index=0,
                    call_id=None,
                    name="read_file",
                    arguments="{}",
                    finish_reason="tool_calls",
                )
            ]
        )
    )
    provider = OpenAIProvider(
        openai_config,
        "test-secret",
        client=make_client(create),
    )

    with pytest.raises(ProviderError, match="不完整的工具调用"):
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
