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
from mewcode_agent.providers.anthropic_provider import AnthropicProvider
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


def test_anthropic_serializes_context_summary_and_boundary_as_user_blocks() -> None:
    request = ProviderRequest(
        "system",
        (
            ContextSummaryMessage(1, 2, '{"schema_version":1}'),
            ContextBoundaryMessage(1, "重新读取精确内容"),
        ),
        None,
    )

    messages = AnthropicProvider._request_messages(request)

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    blocks = messages[0]["content"]
    assert "<mewcode-summary" in blocks[0]["text"]
    assert "<mewcode-boundary" in blocks[1]["text"]


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


def message_delta(
    stop_reason: str | None,
    usage: Any | None = None,
) -> Any:
    return SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason=stop_reason),
        usage=usage,
    )


def request_for(
    *items: ChatMessage | ControlMessage,
) -> ProviderRequest:
    return ProviderRequest("system text", tuple(items), None)


async def collect(provider: AnthropicProvider) -> list[ProviderStreamEvent]:
    return [
        event
        async for event in provider.stream_chat(
            request_for(ChatMessage(role="user", content="你好"))
        )
    ]


ANTHROPIC_USAGE_MISSING_EVENT = ProviderUsageEvent(
    ProviderUsageResult(
        "unavailable",
        None,
        "anthropic_usage_missing",
    )
)


def anthropic_usage(**overrides: object) -> Any:
    fields: dict[str, object] = {
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1536,
        "input_tokens": 7,
        "output_tokens": 13,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


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
        ANTHROPIC_USAGE_MISSING_EVENT,
        ProviderTurnEnd("end_turn"),
    ]
    assert stream.kwargs == {
        "model": "deepseek-v4-pro",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "你好"}],
            }
        ],
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
        ANTHROPIC_USAGE_MISSING_EVENT,
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
            ProviderRequest(
                "system text",
                (ChatMessage(role="user", content="读取 README"),),
                tuple(tools),
            )
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
        ANTHROPIC_USAGE_MISSING_EVENT,
        ProviderTurnEnd("tool_calls"),
    ]
    assert stream.kwargs == {
        "model": "deepseek-v4-pro",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "读取 README"}
                ],
            }
        ],
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
            ProviderRequest(
                "system text",
                (ChatMessage(role="user", content="读取两个文件"),),
                (
                    {
                        "name": "read_file",
                        "input_schema": {"type": "object"},
                    },
                ),
            )
        )
    ]

    assert result == [
        ProviderToolCall(ToolCall("toolu_1", "read_file", '{"path":"one"}')),
        ProviderToolCall(ToolCall("toolu_2", "read_file", '{"path":"two"}')),
        ANTHROPIC_USAGE_MISSING_EVENT,
        ProviderTurnEnd("tool_calls"),
    ]


def test_anthropic_provider_serializes_thinking_before_tool_use() -> None:
    first_call = ToolCall("toolu_1", "read_file", '{"path":"one"}')
    second_call = ToolCall("toolu_2", "read_file", '{"path":"two"}')

    request = AnthropicProvider._request_messages(
        ProviderRequest(
            "system text",
            (
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
            ),
            None,
        )
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


def test_anthropic_controls_merge_only_into_user_content_blocks() -> None:
    call = ToolCall("toolu_1", "read_file", '{"path":"a"}')
    controls = [
        ControlMessage(
            f"runtime.rule_{sequence}",
            "instruction",
            "round",
            f"rule {sequence}",
            sequence,
            anchor,
            1,
            1,
        )
        for sequence, anchor in ((1, 0), (2, 1), (3, 3), (4, 4))
    ]
    request = ProviderRequest(
        "stable",
        (
            controls[0],
            ChatMessage(role="user", content="任务"),
            controls[1],
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=(call,),
            ),
            ChatMessage(
                role="tool",
                content='{"success":true}',
                tool_call_id="toolu_1",
            ),
            controls[2],
            ChatMessage(role="assistant", content="继续"),
            controls[3],
        ),
        None,
    )

    messages = AnthropicProvider._request_messages(request)

    assert [item["role"] for item in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    first_types = [block["type"] for block in messages[0]["content"]]
    assert first_types == ["text", "text", "text"]
    assert messages[0]["content"][1] == {"type": "text", "text": "任务"}
    tool_types = [block["type"] for block in messages[2]["content"]]
    assert tool_types == ["tool_result", "text"]
    assert messages[3] == {"role": "assistant", "content": "继续"}
    assert messages[4]["content"][0]["text"].startswith(
        "<mewcode-control\n"
    )
    assert all(
        not (
            item["role"] == "assistant"
            and isinstance(item["content"], list)
            and any(
                block.get("type") == "text"
                and block.get("text", "").startswith("<mewcode-control")
                for block in item["content"]
            )
        )
        for item in messages
    )


def test_anthropic_merges_adjacent_user_messages() -> None:
    request = ProviderRequest(
        "stable",
        (
            ChatMessage(role="user", content="first"),
            ChatMessage(role="user", content="second"),
        ),
        None,
    )

    assert AnthropicProvider._request_messages(request) == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ],
        }
    ]


def test_control_after_assistant_creates_synthetic_user_message() -> None:
    message = ControlMessage(
        "runtime.rule_1",
        "instruction",
        "round",
        "rule",
        1,
        1,
        1,
        1,
    )
    request = ProviderRequest(
        "stable",
        (ChatMessage(role="assistant", content="answer"), message),
        None,
    )

    result = AnthropicProvider._request_messages(request)

    assert result[0] == {"role": "assistant", "content": "answer"}
    assert result[1]["role"] == "user"
    assert result[1]["content"][0]["type"] == "text"
    assert result[1]["content"][0]["text"].startswith(
        "<mewcode-control\n"
    )


@pytest.mark.asyncio
async def test_anthropic_uses_only_final_message_delta_usage(
    anthropic_config: ProviderConfig,
) -> None:
    events = [
        SimpleNamespace(
            type="message_start",
            usage=anthropic_usage(
                cache_read_input_tokens=0,
                input_tokens=1543,
                output_tokens=0,
            ),
        ),
        text_delta("OK"),
        message_delta("end_turn", usage=anthropic_usage()),
    ]
    stream = FakeAnthropicStream(FakeAnthropicEventManager(events))
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    result = await collect(provider)

    assert result[-2:] == [
        ProviderUsageEvent(
            ProviderUsageResult(
                "available",
                ProviderUsage(1543, 1536, 7, 13),
                None,
            )
        ),
        ProviderTurnEnd("end_turn"),
    ]


@pytest.mark.parametrize(
    ("raw", "status", "reason"),
    [
        (
            anthropic_usage(cache_creation_input_tokens=None),
            "available",
            None,
        ),
        (
            anthropic_usage(cache_creation_input_tokens=5),
            "invalid",
            "anthropic_cache_creation_nonzero",
        ),
        (
            anthropic_usage(input_tokens=None),
            "invalid",
            "anthropic_usage_fields_missing",
        ),
    ],
)
def test_anthropic_usage_edge_cases(
    raw: Any,
    status: str,
    reason: str | None,
) -> None:
    result = AnthropicProvider._usage_result(raw)
    assert result.status == status
    assert result.reason == reason


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

    assert await collect(provider) == [
        ANTHROPIC_USAGE_MISSING_EVENT,
        ProviderTurnEnd("other"),
    ]


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
        ANTHROPIC_USAGE_MISSING_EVENT,
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
