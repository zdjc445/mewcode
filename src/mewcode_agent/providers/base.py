"""Common provider interface and safe errors."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall

ProviderProtocol: TypeAlias = Literal["openai", "anthropic"]
ProviderStopReason: TypeAlias = Literal[
    "end_turn",
    "tool_calls",
    "max_tokens",
    "other",
]


@dataclass(frozen=True, slots=True)
class ProviderThinkingDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ProviderThinkingComplete:
    block: ThinkingBlock


@dataclass(frozen=True, slots=True)
class ProviderTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    tool_call: ToolCall


@dataclass(frozen=True, slots=True)
class ProviderTurnEnd:
    stop_reason: ProviderStopReason


ProviderStreamEvent: TypeAlias = (
    ProviderThinkingDelta
    | ProviderThinkingComplete
    | ProviderTextDelta
    | ProviderToolCall
    | ProviderTurnEnd
)


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
        system_prompt: str,
    ) -> AsyncIterator[ProviderStreamEvent]: ...
