"""Immutable models for declarative lifecycle hooks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Literal, TypeAlias


HookSource: TypeAlias = Literal["user", "project"]
HookEventName: TypeAlias = Literal[
    "system.startup",
    "system.shutdown",
    "system.error",
    "context.before_compaction",
    "context.after_compaction",
    "session.started",
    "session.ended",
    "round.started",
    "round.ended",
    "message.before_send",
    "message.after_receive",
    "tool.before_execute",
    "tool.after_execute",
]
HookMatcherKind: TypeAlias = Literal["exact", "glob", "regex", "not"]
HookConditionMode: TypeAlias = Literal["all", "any"]
HookActionType: TypeAlias = Literal["shell", "prompt", "http", "subagent"]

HOOK_EVENT_NAMES: tuple[HookEventName, ...] = (
    "system.startup",
    "system.shutdown",
    "system.error",
    "context.before_compaction",
    "context.after_compaction",
    "session.started",
    "session.ended",
    "round.started",
    "round.ended",
    "message.before_send",
    "message.after_receive",
    "tool.before_execute",
    "tool.after_execute",
)

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")


class HookConfigError(RuntimeError):
    """A safe, user-facing Hook configuration error."""


@dataclass(frozen=True, slots=True)
class HookValueMatcher:
    kind: HookMatcherKind
    pattern: str | int | float | bool | None | HookValueMatcher

    def __post_init__(self) -> None:
        if self.kind not in ("exact", "glob", "regex", "not"):
            raise ValueError("matcher kind 无效")
        if self.kind == "not":
            if not isinstance(self.pattern, HookValueMatcher):
                raise ValueError("not matcher pattern 必须是 matcher")
            return
        if isinstance(self.pattern, HookValueMatcher):
            raise ValueError("非 not matcher pattern 不能是 matcher")
        if self.kind in ("glob", "regex"):
            if not isinstance(self.pattern, str) or not self.pattern:
                raise ValueError(f"{self.kind} matcher pattern 必须为非空字符串")
        elif type(self.pattern) not in (str, int, float, bool, type(None)):
            raise ValueError("exact matcher pattern 必须是 JSON scalar")
        if isinstance(self.pattern, float) and not math.isfinite(self.pattern):
            raise ValueError("matcher pattern 不能是非有限浮点数")


@dataclass(frozen=True, slots=True)
class ShellHookAction:
    command: str
    cwd: Literal["project"] = "project"
    action_type: Literal["shell"] = field(default="shell", init=False)


@dataclass(frozen=True, slots=True)
class PromptHookAction:
    content: str
    action_type: Literal["prompt"] = field(default="prompt", init=False)


@dataclass(frozen=True, slots=True)
class HttpHookAction:
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    url: str
    headers: Mapping[str, str]
    body: str
    action_type: Literal["http"] = field(default="http", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True, slots=True)
class SubagentHookAction:
    task: str
    context: Literal["summary", "recent", "none"]
    action_type: Literal["subagent"] = field(default="subagent", init=False)


HookAction: TypeAlias = (
    ShellHookAction | PromptHookAction | HttpHookAction | SubagentHookAction
)


@dataclass(frozen=True, slots=True)
class HookInterception:
    deny: Literal[True]
    reason: str


@dataclass(frozen=True, slots=True)
class HookCondition:
    mode: HookConditionMode
    matchers: Mapping[str, HookValueMatcher]

    def __post_init__(self) -> None:
        if self.mode not in ("all", "any"):
            raise ValueError("condition mode 无效")
        if not isinstance(self.matchers, Mapping) or not self.matchers:
            raise ValueError("condition matchers 必须是非空 mapping")
        copied = dict(self.matchers)
        for path, matcher in copied.items():
            if not isinstance(path, str) or not _IDENTIFIER.fullmatch(path):
                raise ValueError("matcher 字段路径格式无效")
            if not isinstance(matcher, HookValueMatcher):
                raise ValueError("condition matchers 包含无效对象")
        object.__setattr__(self, "matchers", MappingProxyType(copied))


@dataclass(frozen=True, slots=True)
class HookRule:
    rule_id: str
    source: HookSource
    event: HookEventName
    once: bool
    run_async: bool
    timeout_seconds: float
    condition: HookCondition | None
    action: HookAction
    interception: HookInterception | None

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not _IDENTIFIER.fullmatch(
            self.rule_id
        ):
            raise ValueError("rule id 格式无效")
        if self.source not in ("user", "project"):
            raise ValueError("rule source 无效")
        if self.event not in HOOK_EVENT_NAMES:
            raise ValueError("rule event 无效")
        if type(self.once) is not bool:
            raise ValueError("rule once 必须是 bool")
        if type(self.run_async) is not bool:
            raise ValueError("rule async 必须是 bool")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or not 0 < float(self.timeout_seconds) <= 300
        ):
            raise ValueError("rule timeout_seconds 必须大于 0 且不超过 300")
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        if self.condition is not None and not isinstance(
            self.condition,
            HookCondition,
        ):
            raise ValueError("rule condition 无效")
        if not isinstance(
            self.action,
            (
                ShellHookAction,
                PromptHookAction,
                HttpHookAction,
                SubagentHookAction,
            ),
        ):
            raise ValueError("rule action 无效")
        if self.run_async and isinstance(self.action, PromptHookAction):
            raise ValueError("prompt action 不能异步执行")
        if isinstance(self.action, PromptHookAction) and self.event in (
            "session.ended",
            "system.shutdown",
        ):
            raise ValueError("该事件没有可消费 prompt 的后续 request")
        if self.interception is not None:
            if not isinstance(self.interception, HookInterception):
                raise ValueError("rule interception 无效")
            if self.event != "tool.before_execute":
                raise ValueError("intercept 只允许 tool.before_execute")
            if self.run_async:
                raise ValueError("intercept 不能异步执行")


@dataclass(frozen=True, slots=True)
class HookConfiguration:
    rules: tuple[HookRule, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rules, tuple) or any(
            not isinstance(rule, HookRule) for rule in self.rules
        ):
            raise ValueError("rules 必须是 HookRule tuple")
        identifiers = [rule.rule_id for rule in self.rules]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("生效 Hook 规则 id 不能重复")


@dataclass(frozen=True, slots=True)
class HookDiagnostic:
    source: HookSource | None
    rule_id: str | None
    event: HookEventName
    action_type: HookActionType | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class HookDispatchResult:
    blocked: bool = False
    block_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.blocked) is not bool:
            raise ValueError("blocked 必须是 bool")
        if self.blocked != (self.block_reason is not None):
            raise ValueError("blocked 与 block_reason 不一致")


@dataclass(frozen=True, slots=True)
class HookCloseResult:
    background_tasks_waited: int
    pending_prompts_discarded: int

    def __post_init__(self) -> None:
        if (
            type(self.background_tasks_waited) is not int
            or self.background_tasks_waited < 0
            or type(self.pending_prompts_discarded) is not int
            or self.pending_prompts_discarded < 0
        ):
            raise ValueError("关闭统计必须是非负整数")


def require_absolute_project_root(project_root: Path) -> Path:
    if not isinstance(project_root, Path) or not project_root.is_absolute():
        raise ValueError("project_root 必须是绝对 Path")
    return project_root


def validate_context_path(path: str) -> bool:
    return bool(_IDENTIFIER.fullmatch(path))


def action_type(action: HookAction) -> HookActionType:
    return action.action_type


HookContext: TypeAlias = Mapping[str, Any]
