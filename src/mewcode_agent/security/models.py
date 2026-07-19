"""Immutable security policy inputs, rules, and decisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Literal, TypeAlias


SecurityAction: TypeAlias = Literal["allow", "deny", "ask"]
PermissionMode: TypeAlias = Literal["strict", "default", "permissive"]
RuleScope: TypeAlias = Literal["session", "project", "user"]
MatcherKind: TypeAlias = Literal["exact", "glob", "path_glob"]
SecurityCategory: TypeAlias = Literal["read", "write", "command"]

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]*")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class SecurityRequest:
    call_id: str
    tool_name: str
    category: SecurityCategory
    arguments: Mapping[str, Any] = field(repr=False)
    working_directory: Path = field(default_factory=Path.cwd)
    current_request_authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id:
            raise ValueError("call_id 必须为非空字符串")
        if not isinstance(self.tool_name, str) or not _TOOL_NAME.fullmatch(
            self.tool_name
        ):
            raise ValueError("tool_name 格式无效")
        if self.category not in ("read", "write", "command"):
            raise ValueError("category 无效")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("arguments 必须为映射")
        object.__setattr__(
            self,
            "arguments",
            MappingProxyType(dict(self.arguments)),
        )
        if not isinstance(self.working_directory, Path):
            raise ValueError("working_directory 必须为 Path")
        if not self.working_directory.is_absolute():
            raise ValueError("working_directory 必须为绝对路径")
        if type(self.current_request_authorized) is not bool:
            raise ValueError("current_request_authorized 必须为 bool")


@dataclass(frozen=True, slots=True)
class ArgumentMatcher:
    argument: str
    kind: MatcherKind
    pattern: str | int | bool

    def __post_init__(self) -> None:
        if not isinstance(self.argument, str) or not _TOOL_NAME.fullmatch(
            self.argument
        ):
            raise ValueError("matcher argument 格式无效")
        if self.kind not in ("exact", "glob", "path_glob"):
            raise ValueError("matcher kind 无效")
        if self.kind in ("glob", "path_glob"):
            if not isinstance(self.pattern, str) or not self.pattern:
                raise ValueError("glob matcher pattern 必须为非空字符串")
        elif type(self.pattern) not in (str, int, bool):
            raise ValueError("exact matcher pattern 类型无效")


@dataclass(frozen=True, slots=True)
class SecurityRule:
    rule_id: str
    scope: RuleScope
    priority: int
    action: SecurityAction
    tool_name: str
    matchers: tuple[ArgumentMatcher, ...] = ()
    fingerprint: str | None = None
    project_root: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not _IDENTIFIER.fullmatch(
            self.rule_id
        ):
            raise ValueError("rule_id 格式无效")
        if self.scope not in ("session", "project", "user"):
            raise ValueError("rule scope 无效")
        if type(self.priority) is not int:
            raise ValueError("rule priority 必须为整数")
        if self.action not in ("allow", "deny", "ask"):
            raise ValueError("rule action 无效")
        if not isinstance(self.tool_name, str) or not _TOOL_NAME.fullmatch(
            self.tool_name
        ):
            raise ValueError("rule tool_name 格式无效")
        if not isinstance(self.matchers, tuple):
            raise ValueError("rule matchers 必须为 tuple")
        if any(
            not isinstance(matcher, ArgumentMatcher)
            for matcher in self.matchers
        ):
            raise ValueError("rule matchers 包含无效对象")
        arguments = [matcher.argument for matcher in self.matchers]
        if len(arguments) != len(set(arguments)):
            raise ValueError("同一规则不能重复匹配同一参数")
        if self.fingerprint is not None:
            if not isinstance(
                self.fingerprint,
                str,
            ) or not _FINGERPRINT.fullmatch(self.fingerprint):
                raise ValueError("rule fingerprint 格式无效")
            if self.matchers:
                raise ValueError("fingerprint 规则不能同时声明 matchers")
        if self.project_root is not None:
            if not isinstance(self.project_root, str) or not Path(
                self.project_root
            ).is_absolute():
                raise ValueError("project_root 必须为绝对路径字符串")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: SecurityAction
    reason_code: str
    rule_id: str | None = None
    scope: RuleScope | None = None

    def __post_init__(self) -> None:
        if self.action not in ("allow", "deny", "ask"):
            raise ValueError("decision action 无效")
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise ValueError("decision reason_code 必须为非空字符串")
        if (self.rule_id is None) != (self.scope is None):
            raise ValueError("decision rule_id 与 scope 必须同时存在或缺失")


@dataclass(frozen=True, slots=True)
class SecurityConfiguration:
    mode: PermissionMode
    user_rules: tuple[SecurityRule, ...]
    project_rules: tuple[SecurityRule, ...]
    permanent_rules: tuple[SecurityRule, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in ("strict", "default", "permissive"):
            raise ValueError("permission mode 无效")
        if any(rule.scope != "user" for rule in self.user_rules):
            raise ValueError("user_rules 中存在非 user 规则")
        if any(rule.scope != "project" for rule in self.project_rules):
            raise ValueError("project_rules 中存在非 project 规则")
        if any(rule.scope != "user" for rule in self.permanent_rules):
            raise ValueError("permanent_rules 中存在非 user 规则")
