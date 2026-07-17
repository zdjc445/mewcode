"""DeepSeek adapter using the OpenAI-compatible protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import openai
from openai import AsyncOpenAI

from mewcode_agent.config import ProviderConfig
from mewcode_agent.models import ChatMessage, ToolCall
from mewcode_agent.providers.base import ProviderError, StreamPart


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
    def protocol(self) -> str:
        return "openai"

    @staticmethod
    def _request_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        request_messages: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                request_messages.append(
                    {
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
                )
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
                stream=True,
            )
            if tools:
                request["tools"] = tools
            stream = await self._client.chat.completions.create(**request)
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    if text.strip():
                        received_text = True
                    yield text
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
                raise ProviderError("OpenAI 兼容接口返回了空响应")
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
