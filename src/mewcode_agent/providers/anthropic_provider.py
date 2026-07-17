"""DeepSeek adapter using the Anthropic-compatible protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from mewcode_agent.config import ProviderConfig
from mewcode_agent.models import ChatMessage, ToolCall
from mewcode_agent.providers.base import ProviderError, StreamPart


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
    def protocol(self) -> str:
        return "anthropic"

    @staticmethod
    def _request_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        request_messages: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                content: list[dict[str, Any]] = []
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
            elif message.role == "tool":
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
                previous = request_messages[-1] if request_messages else None
                if (
                    previous is not None
                    and previous["role"] == "user"
                    and isinstance(previous["content"], list)
                    and all(
                        block.get("type") == "tool_result"
                        for block in previous["content"]
                    )
                ):
                    previous["content"].append(result_block)
                else:
                    request_messages.append(
                        {"role": "user", "content": [result_block]}
                    )
            else:
                request_messages.append(
                    {"role": message.role, "content": message.content}
                )
        return request_messages

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamPart]:
        request_messages = self._request_messages(messages)
        received_text = False
        streamed_tool_calls: dict[int, dict[str, str]] = {}

        try:
            request: dict[str, Any] = dict(
                model=self._config.model,
                messages=request_messages,
                max_tokens=self._config.max_tokens,
            )
            if tools:
                request["tools"] = tools
                request["tool_choice"] = {"type": "auto"}
            async with self._client.messages.stream(**request) as stream:
                if not tools:
                    async for text in stream.text_stream:
                        if text:
                            if text.strip():
                                received_text = True
                            yield text
                else:
                    async for event in stream:
                        if event.type == "content_block_start":
                            block = event.content_block
                            if block.type == "tool_use":
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
                                if delta.text.strip():
                                    received_text = True
                                yield delta.text
                            elif delta.type == "input_json_delta":
                                current = streamed_tool_calls.get(event.index)
                                if current is None:
                                    raise ProviderError(
                                        "Anthropic 兼容接口返回了无起始块的工具参数"
                                    )
                                current["arguments"] += delta.partial_json

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
                    yield tool_call
            elif not received_text:
                raise ProviderError("Anthropic 兼容接口返回了空响应")
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
