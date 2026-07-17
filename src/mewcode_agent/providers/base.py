"""Common provider interface and safe errors."""

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, TypeAlias

from mewcode_agent.models import ChatMessage, ToolCall

ProviderProtocol: TypeAlias = Literal["openai", "anthropic"]
StreamPart: TypeAlias = str | ToolCall


class ProviderError(RuntimeError):
    """A sanitized provider failure safe to display in the TUI."""


class LLMProvider(Protocol):
    """The SDK-independent streaming interface consumed by the UI."""

    @property
    def protocol(self) -> ProviderProtocol: ...

    def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamPart]: ...
