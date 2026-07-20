"""DeepSeek adapter using the Anthropic-compatible protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

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

ANTHROPIC_STOP_REASON_MAP: dict[str, ProviderStopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_calls",
    "max_tokens": "max_tokens",
}


class AnthropicProvider:
    def __init__(
        self,
        config: ProviderConfig,
        api_key: str,
        *,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._client = client or AsyncAnthropic(
            api_key=api_key,
            base_url=config.base_url,
        )

    @property
    def provider_id(self) -> str:
        return self._config.provider_id

    @property
    def protocol(self) -> ProviderProtocol:
        return "anthropic"

    @staticmethod
    def _append_user_blocks(
        messages: list[dict[str, Any]],
        blocks: list[dict[str, Any]],
    ) -> None:
        if messages and messages[-1]["role"] == "user":
            current = messages[-1]["content"]
            if isinstance(current, str):
                current = [{"type": "text", "text": current}]
                messages[-1]["content"] = current
            current.extend(blocks)
        else:
            messages.append({"role": "user", "content": list(blocks)})

    @staticmethod
    def _request_messages(request: ProviderRequest) -> list[dict[str, Any]]:
        request_messages: list[dict[str, Any]] = []
        for item in request.items:
            if isinstance(item, ControlMessage):
                AnthropicProvider._append_user_blocks(
                    request_messages,
                    [
                        {
                            "type": "text",
                            "text": render_control_message(item),
                        }
                    ],
                )
                continue
            if isinstance(item, ContextSummaryMessage):
                AnthropicProvider._append_user_blocks(
                    request_messages,
                    [
                        {
                            "type": "text",
                            "text": render_context_summary(item),
                        }
                    ],
                )
                continue
            if isinstance(item, ContextBoundaryMessage):
                AnthropicProvider._append_user_blocks(
                    request_messages,
                    [
                        {
                            "type": "text",
                            "text": render_context_boundary(item),
                        }
                    ],
                )
                continue
            message = item
            if message.role == "user":
                AnthropicProvider._append_user_blocks(
                    request_messages,
                    [{"type": "text", "text": message.content}],
                )
            elif message.role == "tool":
                AnthropicProvider._append_user_blocks(
                    request_messages,
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id,
                            "content": message.content,
                        }
                    ],
                )
            elif message.role == "assistant" and message.tool_calls:
                content: list[dict[str, Any]] = []
                for block in message.thinking_blocks:
                    content.append(
                        {
                            "type": "thinking",
                            "thinking": block.text,
                            "signature": block.signature,
                        }
                    )
                if message.content:
                    content.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    try:
                        arguments = json.loads(call.arguments_json)
                    except json.JSONDecodeError:
                        arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}
                    content.append(
                        {
                            "type": "tool_use",
                            "id": call.call_id,
                            "name": call.name,
                            "input": arguments,
                        }
                    )
                request_messages.append({"role": "assistant", "content": content})
            else:
                request_messages.append(
                    {"role": "assistant", "content": message.content}
                )
        return request_messages

    def prompt_payload(self, request: ProviderRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": self._request_messages(request),
            "max_tokens": self._config.max_tokens,
            "system": request.system_prompt,
        }
        if request.tools:
            payload["tools"] = list(request.tools)
            payload["tool_choice"] = {"type": "auto"}
        return payload

    @staticmethod
    def _usage_result(raw: Any | None) -> ProviderUsageResult:
        if raw is None:
            return ProviderUsageResult(
                "unavailable",
                None,
                "anthropic_usage_missing",
            )
        cache_creation = getattr(
            raw,
            "cache_creation_input_tokens",
            None,
        )
        if cache_creation not in (None, 0):
            return ProviderUsageResult(
                "invalid",
                None,
                "anthropic_cache_creation_nonzero",
            )
        cache_read = getattr(raw, "cache_read_input_tokens", None)
        input_tokens = getattr(raw, "input_tokens", None)
        output_tokens = getattr(raw, "output_tokens", None)
        if any(
            value is None
            for value in (cache_read, input_tokens, output_tokens)
        ):
            return ProviderUsageResult(
                "invalid",
                None,
                "anthropic_usage_fields_missing",
            )
        try:
            usage = ProviderUsage(
                prompt_tokens=cache_read + input_tokens,
                cache_hit_tokens=cache_read,
                cache_miss_tokens=input_tokens,
                completion_tokens=output_tokens,
            )
        except (TypeError, ValueError):
            return ProviderUsageResult(
                "invalid",
                None,
                "anthropic_usage_invalid",
            )
        return ProviderUsageResult("available", usage, None)

    async def stream_chat(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[ProviderStreamEvent]:
        thinking_blocks: dict[int, dict[str, str]] = {}
        streamed_tool_calls: dict[int, dict[str, str]] = {}
        stop_reason: str | None = None
        final_usage: Any | None = None

        try:
            sdk_request = self.prompt_payload(request)
            async with self._client.messages.stream(**sdk_request) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "thinking":
                            initial_text = (
                                getattr(block, "thinking", "") or ""
                            )
                            thinking_blocks[event.index] = {
                                "text": initial_text,
                                "signature": (
                                    getattr(block, "signature", "") or ""
                                ),
                            }
                            if initial_text:
                                yield ProviderThinkingDelta(initial_text)
                        elif block.type == "tool_use":
                            initial_input = ""
                            if block.input:
                                initial_input = json.dumps(
                                    block.input,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                            streamed_tool_calls[event.index] = {
                                "id": block.id,
                                "name": block.name,
                                "arguments": initial_input,
                            }
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta" and delta.text:
                            yield ProviderTextDelta(delta.text)
                        elif delta.type == "thinking_delta":
                            current = thinking_blocks.get(event.index)
                            if current is None:
                                raise ProviderError(
                                    "Anthropic 兼容接口返回了无起始块的 thinking"
                                )
                            if delta.thinking:
                                current["text"] += delta.thinking
                                yield ProviderThinkingDelta(delta.thinking)
                        elif delta.type == "signature_delta":
                            current = thinking_blocks.get(event.index)
                            if current is None:
                                raise ProviderError(
                                    "Anthropic 兼容接口返回了无起始块的 signature"
                                )
                            current["signature"] += delta.signature
                        elif delta.type == "input_json_delta":
                            current = streamed_tool_calls.get(event.index)
                            if current is None:
                                raise ProviderError(
                                    "Anthropic 兼容接口返回了无起始块的工具参数"
                                )
                            current["arguments"] += delta.partial_json
                    elif event.type == "content_block_stop":
                        raw_block = thinking_blocks.pop(event.index, None)
                        if (
                            raw_block is not None
                            and raw_block["text"].strip()
                        ):
                            yield ProviderThinkingComplete(
                                ThinkingBlock(
                                    raw_block["text"],
                                    raw_block["signature"],
                                )
                            )
                    elif event.type == "message_delta":
                        final_usage = getattr(event, "usage", None)
                        stop_reason = (
                            getattr(event.delta, "stop_reason", None)
                            or stop_reason
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
            yield ProviderUsageEvent(self._usage_result(final_usage))
            yield ProviderTurnEnd(
                ANTHROPIC_STOP_REASON_MAP.get(stop_reason or "", "other")
            )
        except ProviderError:
            raise
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Anthropic 兼容接口鉴权失败") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("Anthropic 兼容接口触发限流") from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderError("Anthropic 兼容接口请求超时") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("无法连接 Anthropic 兼容接口") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                f"Anthropic 兼容接口请求失败（HTTP {exc.status_code}）"
            ) from exc
        except anthropic.APIError as exc:
            raise ProviderError("Anthropic 兼容接口请求失败") from exc
        except Exception as exc:
            raise ProviderError("Anthropic 兼容接口流式响应中断") from exc
