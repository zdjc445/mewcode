"""DeepSeek adapter using the OpenAI-compatible protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import openai
from openai import AsyncOpenAI

from mewcode_agent.config import ProviderConfig
from mewcode_agent.models import ThinkingBlock, ToolCall
from mewcode_agent.prompting.composer import (
    render_context_boundary,
    render_context_summary,
    render_control_message,
)
from mewcode_agent.prompting.models import (
    ControlMessage,
    ContextBoundaryMessage,
    ContextSummaryMessage,
)
from mewcode_agent.providers.base import (
    ProviderError,
    ProviderProtocol,
    ProviderRequest,
    ProviderStopReason,
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

OPENAI_STOP_REASON_MAP: dict[str, ProviderStopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_calls",
    "length": "max_tokens",
}


class OpenAIProvider:
    def __init__(
        self,
        config: ProviderConfig,
        api_key: str,
        *,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=config.base_url,
        )

    @property
    def provider_id(self) -> str:
        return self._config.provider_id

    @property
    def protocol(self) -> ProviderProtocol:
        return "openai"

    @staticmethod
    def _request_messages(
        request: ProviderRequest,
    ) -> list[dict[str, Any]]:
        request_messages: list[dict[str, Any]] = [
            {"role": "system", "content": request.system_prompt}
        ]
        for message in request.items:
            if isinstance(message, ControlMessage):
                request_messages.append(
                    {
                        "role": "system",
                        "content": render_control_message(message),
                    }
                )
                continue
            if isinstance(message, ContextSummaryMessage):
                request_messages.append(
                    {
                        "role": "system",
                        "content": render_context_summary(message),
                    }
                )
                continue
            if isinstance(message, ContextBoundaryMessage):
                request_messages.append(
                    {
                        "role": "system",
                        "content": render_context_boundary(message),
                    }
                )
                continue
            if message.role == "assistant" and message.tool_calls:
                payload: dict[str, Any] = {
                    "role": "assistant",
                    "content": message.content or None,
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.arguments_json,
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
                if message.thinking_blocks:
                    payload["reasoning_content"] = "".join(
                        block.text for block in message.thinking_blocks
                    )
                request_messages.append(payload)
            elif message.role == "tool":
                request_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
            else:
                request_messages.append(
                    {"role": message.role, "content": message.content}
                )
        return request_messages

    def prompt_payload(self, request: ProviderRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": self._request_messages(request),
            "max_tokens": self._config.max_tokens,
        }
        if request.tools:
            payload["tools"] = list(request.tools)
        return payload

    @staticmethod
    def _usage_result(raw_usage: Any | None) -> ProviderUsageResult:
        if raw_usage is None:
            return ProviderUsageResult(
                "unavailable",
                None,
                "openai_usage_missing",
            )
        field_names = (
            "prompt_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
            "completion_tokens",
        )
        values = tuple(
            getattr(raw_usage, name, None) for name in field_names
        )
        if any(value is None for value in values):
            return ProviderUsageResult(
                "invalid",
                None,
                "openai_usage_fields_missing",
            )
        if any(type(value) is not int or value < 0 for value in values):
            return ProviderUsageResult(
                "invalid",
                None,
                "openai_usage_invalid",
            )
        try:
            usage = ProviderUsage(*values)
        except ValueError:
            return ProviderUsageResult(
                "invalid",
                None,
                "openai_usage_invalid",
            )
        return ProviderUsageResult("available", usage, None)

    async def stream_chat(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[ProviderStreamEvent]:
        reasoning_parts: list[str] = []
        streamed_tool_calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        raw_usage: Any | None = None

        try:
            sdk_request = self.prompt_payload(request)
            sdk_request["stream"] = True
            sdk_request["stream_options"] = {"include_usage": True}
            stream = await self._client.chat.completions.create(**sdk_request)
            async for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    raw_usage = chunk_usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield ProviderThinkingDelta(reasoning)
                text = getattr(delta, "content", None)
                if text:
                    yield ProviderTextDelta(text)
                for fragment in getattr(delta, "tool_calls", None) or []:
                    current = streamed_tool_calls.setdefault(
                        fragment.index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    if fragment.id:
                        current["id"] += fragment.id
                    function = fragment.function
                    if function is not None:
                        if function.name:
                            current["name"] += function.name
                        if function.arguments:
                            current["arguments"] += function.arguments
                finish_reason = (
                    getattr(choice, "finish_reason", None) or finish_reason
                )

            reasoning_text = "".join(reasoning_parts)
            if reasoning_text.strip():
                yield ProviderThinkingComplete(
                    ThinkingBlock(reasoning_text)
                )
            if streamed_tool_calls:
                try:
                    tool_calls = tuple(
                        ToolCall(
                            call_id=raw_call["id"],
                            name=raw_call["name"],
                            arguments_json=raw_call["arguments"],
                        )
                        for _, raw_call in sorted(streamed_tool_calls.items())
                    )
                except ValueError as exc:
                    raise ProviderError("模型返回了不完整的工具调用") from exc
                for tool_call in tool_calls:
                    yield ProviderToolCall(tool_call)
            yield ProviderUsageEvent(self._usage_result(raw_usage))
            yield ProviderTurnEnd(
                OPENAI_STOP_REASON_MAP.get(finish_reason or "", "other")
            )
        except ProviderError:
            raise
        except openai.AuthenticationError as exc:
            raise ProviderError("OpenAI 兼容接口鉴权失败") from exc
        except openai.RateLimitError as exc:
            raise ProviderError("OpenAI 兼容接口触发限流") from exc
        except openai.APITimeoutError as exc:
            raise ProviderError("OpenAI 兼容接口请求超时") from exc
        except openai.APIConnectionError as exc:
            raise ProviderError("无法连接 OpenAI 兼容接口") from exc
        except openai.APIStatusError as exc:
            raise ProviderError(
                f"OpenAI 兼容接口请求失败（HTTP {exc.status_code}）"
            ) from exc
        except openai.APIError as exc:
            raise ProviderError("OpenAI 兼容接口请求失败") from exc
        except Exception as exc:
            raise ProviderError("OpenAI 兼容接口流式响应中断") from exc
