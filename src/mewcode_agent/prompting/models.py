"""Validated data models for static prompts and runtime controls."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Literal, TypeAlias

from mewcode_agent.models import ChatMessage

PromptModuleSource: TypeAlias = Literal["builtin", "user", "project"]
InstructionScope: TypeAlias = Literal["session", "request", "round"]
ControlKind: TypeAlias = Literal["state", "instruction", "context"]

PROMPT_IDENTIFIER_PATTERN = re.compile(
    r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z"
)


def validate_prompt_identifier(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or PROMPT_IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(
            f"{field_name} 必须完整匹配 "
            "[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*"
        )


def _normalized_content(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须为非空字符串")
    return value.strip()


@dataclass(frozen=True, slots=True)
class PromptModule:
    module_id: str
    priority: int
    content: str
    source: PromptModuleSource
    protected: bool

    def __post_init__(self) -> None:
        validate_prompt_identifier(self.module_id, "module_id")
        if type(self.priority) is not int:
            raise ValueError("priority 必须为整数")
        object.__setattr__(
            self,
            "content",
            _normalized_content(self.content, "content"),
        )
        if self.source not in ("builtin", "user", "project"):
            raise ValueError("source 必须为 builtin、user 或 project")
        if type(self.protected) is not bool:
            raise ValueError("protected 必须为布尔值")
        if self.source != "builtin" and self.protected:
            raise ValueError("外部 Prompt 模块不能设置 protected=True")


@dataclass(frozen=True, slots=True)
class RuntimeInstruction:
    instruction_id: str
    kind: ControlKind
    scope: InstructionScope
    content: str
    source: str

    def __post_init__(self) -> None:
        validate_prompt_identifier(self.instruction_id, "instruction_id")
        if self.kind not in ("state", "instruction", "context"):
            raise ValueError("kind 必须为 state、instruction 或 context")
        if self.scope not in ("session", "request", "round"):
            raise ValueError("scope 必须为 session、request 或 round")
        if self.kind == "state" and self.scope != "round":
            raise ValueError("kind=state 只允许 scope=round")
        object.__setattr__(
            self,
            "content",
            _normalized_content(self.content, "content"),
        )
        object.__setattr__(
            self,
            "source",
            _normalized_content(self.source, "source"),
        )


@dataclass(frozen=True, slots=True)
class ControlMessage:
    instruction_id: str
    kind: ControlKind
    scope: InstructionScope
    content: str
    sequence: int
    anchor: int
    request_sequence: int | None
    round_number: int | None

    def __post_init__(self) -> None:
        validate_prompt_identifier(self.instruction_id, "instruction_id")
        if self.kind not in ("state", "instruction", "context"):
            raise ValueError("kind 必须为 state、instruction 或 context")
        if self.scope not in ("session", "request", "round"):
            raise ValueError("scope 必须为 session、request 或 round")
        if self.kind == "state" and self.scope != "round":
            raise ValueError("kind=state 只允许 scope=round")
        object.__setattr__(
            self,
            "content",
            _normalized_content(self.content, "content"),
        )
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("sequence 必须为大于 0 的整数")
        if type(self.anchor) is not int or self.anchor < 0:
            raise ValueError("anchor 必须为大于或等于 0 的整数")
        if self.scope == "session":
            valid_targets = (
                self.request_sequence is None and self.round_number is None
            )
        elif self.scope == "request":
            valid_targets = (
                type(self.request_sequence) is int
                and self.request_sequence > 0
                and self.round_number is None
            )
        else:
            valid_targets = (
                type(self.request_sequence) is int
                and self.request_sequence > 0
                and type(self.round_number) is int
                and self.round_number > 0
            )
        if not valid_targets:
            raise ValueError(
                "scope 与 request_sequence、round_number 不一致"
            )


@dataclass(frozen=True, slots=True)
class ContextSummaryMessage:
    generation: int
    covered_history_end: int
    content_json: str

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("generation 必须为大于 0 的整数")
        if (
            type(self.covered_history_end) is not int
            or self.covered_history_end <= 0
        ):
            raise ValueError("covered_history_end 必须为大于 0 的整数")
        if not isinstance(self.content_json, str) or not self.content_json:
            raise ValueError("content_json 必须为非空字符串")
        try:
            parsed = json.loads(self.content_json)
        except json.JSONDecodeError as exc:
            raise ValueError("content_json 必须是有效 JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("content_json 根节点必须是 object")


@dataclass(frozen=True, slots=True)
class ContextBoundaryMessage:
    generation: int
    content: str

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("generation 必须为大于 0 的整数")
        object.__setattr__(
            self,
            "content",
            _normalized_content(self.content, "content"),
        )


PromptItem: TypeAlias = (
    ChatMessage
    | ControlMessage
    | ContextSummaryMessage
    | ContextBoundaryMessage
)


@dataclass(frozen=True, slots=True)
class PromptFrame:
    system_prompt: str
    items: tuple[PromptItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "system_prompt",
            _normalized_content(self.system_prompt, "system_prompt"),
        )
