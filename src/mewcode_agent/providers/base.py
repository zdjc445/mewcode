"""Common provider interface and safe errors."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

from mewcode_agent.models import ThinkingBlock, ToolCall
from mewcode_agent.prompting.models import PromptItem

ProviderProtocol: TypeAlias = Literal["openai", "anthropic"]
ProviderStopReason: TypeAlias = Literal[
    "end_turn",
    "tool_calls",
    "max_tokens",
    "other",
]


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    system_prompt: str
    items: tuple[PromptItem, ...]
    tools: tuple[dict[str, Any], ...] | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.system_prompt, str)
            or not self.system_prompt.strip()
        ):
            raise ValueError("system_prompt 必须为非空字符串")
        if not isinstance(self.items, tuple):
            raise ValueError("items 必须为 tuple")
        if self.tools is not None and not isinstance(self.tools, tuple):
            raise ValueError("tools 必须为 tuple 或 None")


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    prompt_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    completion_tokens: int

    def __post_init__(self) -> None:
        values = (
            self.prompt_tokens,
            self.cache_hit_tokens,
            self.cache_miss_tokens,
            self.completion_tokens,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("usage Token 字段必须为非负整数")
        if self.prompt_tokens != self.cache_hit_tokens + self.cache_miss_tokens:
            raise ValueError("prompt_tokens 必须等于 hit 与 miss 之和")


UsageStatus: TypeAlias = Literal["available", "unavailable", "invalid"]


@dataclass(frozen=True, slots=True)
class ProviderUsageResult:
    status: UsageStatus
    usage: ProviderUsage | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.status == "available":
            valid = isinstance(self.usage, ProviderUsage) and self.reason is None
        elif self.status in ("unavailable", "invalid"):
            valid = (
                self.usage is None
                and isinstance(self.reason, str)
                and bool(self.reason.strip())
            )
        else:
            valid = False
        if not valid:
            raise ValueError("usage status、usage 与 reason 不一致")


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


@dataclass(frozen=True, slots=True)
class ProviderUsageEvent:
    result: ProviderUsageResult


ProviderStreamEvent: TypeAlias = (
    ProviderThinkingDelta
    | ProviderThinkingComplete
    | ProviderTextDelta
    | ProviderToolCall
    | ProviderUsageEvent
    | ProviderTurnEnd
)


class ProviderError(RuntimeError):
    """A sanitized provider failure safe to display in the TUI."""


class LLMProvider(Protocol):
    """The SDK-independent streaming interface consumed by the UI."""

    @property
    def provider_id(self) -> str: ...

    @property
    def protocol(self) -> ProviderProtocol: ...

    def prompt_payload(self, request: ProviderRequest) -> dict[str, Any]: ...

    def stream_chat(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[ProviderStreamEvent]: ...
