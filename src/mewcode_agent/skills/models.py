"""Validated models and stable errors for layered Skills."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Literal, TypeAlias


SkillSource: TypeAlias = Literal["builtin", "user", "project"]
SkillExecutionMode: TypeAlias = Literal["shared", "isolated"]
SkillContextStrategy: TypeAlias = Literal[
    "current",
    "summary",
    "recent",
    "none",
]

SKILL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]*\Z")
SKILL_TOOL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class SkillConfigError(RuntimeError):
    """A stable Skill configuration failure safe to show to users."""

    def __init__(self, code: str, message: str) -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("code 必须是非空字符串")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message 必须是非空字符串")
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    source: SkillSource
    candidate: str
    code: str
    message: str

    def __post_init__(self) -> None:
        if self.source not in ("builtin", "user", "project"):
            raise ValueError("source 无效")
        for field_name, value in (
            ("candidate", self.candidate),
            ("code", self.code),
            ("message", self.message),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or "\x00" in value
            ):
                raise ValueError(f"{field_name} 必须是非空字符串")


@dataclass(frozen=True, slots=True)
class SkillToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    category: Literal["command"]
    timeout_seconds: float
    script: str
    script_path: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or SKILL_TOOL_NAME_PATTERN.fullmatch(self.name) is None
        ):
            raise ValueError("name 格式无效")
        if (
            not isinstance(self.description, str)
            or not self.description.strip()
            or any(character in self.description for character in "\r\n\x00")
        ):
            raise ValueError("description 必须是非空单行字符串")
        if not isinstance(self.parameters, dict):
            raise ValueError("parameters 必须是 dict")
        if self.category != "command":
            raise ValueError("category 必须为 command")
        if (
            not isinstance(self.timeout_seconds, float)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 300
        ):
            raise ValueError("timeout_seconds 超出允许范围")
        if not isinstance(self.script, str) or not self.script:
            raise ValueError("script 必须是非空字符串")
        if not isinstance(self.script_path, Path) or not self.script_path.is_absolute():
            raise ValueError("script_path 必须是绝对 Path")


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    name: str
    description: str
    allowed_tools: tuple[str, ...]
    execution_mode: SkillExecutionMode
    model: Literal["inherit"]
    context_strategy: SkillContextStrategy
    recent_messages: int | None
    body: str
    source: SkillSource
    source_root: Path
    source_path: Path
    skill_directory: Path | None
    dedicated_tools: tuple[SkillToolDefinition, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or SKILL_NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError("name 格式无效")
        if (
            not isinstance(self.description, str)
            or not self.description.strip()
            or any(character in self.description for character in "\r\n\x00")
        ):
            raise ValueError("description 必须是非空单行字符串")
        if (
            not isinstance(self.allowed_tools, tuple)
            or any(not isinstance(name, str) or not name for name in self.allowed_tools)
            or len(set(self.allowed_tools)) != len(self.allowed_tools)
        ):
            raise ValueError("allowed_tools 无效")
        if self.execution_mode not in ("shared", "isolated"):
            raise ValueError("execution_mode 无效")
        if self.model != "inherit":
            raise ValueError("model 必须为 inherit")
        if self.context_strategy not in ("current", "summary", "recent", "none"):
            raise ValueError("context_strategy 无效")
        if self.execution_mode == "shared":
            if self.context_strategy != "current" or self.recent_messages is not None:
                raise ValueError("shared 上下文策略无效")
        elif self.context_strategy == "recent":
            if type(self.recent_messages) is not int or self.recent_messages <= 0:
                raise ValueError("recent_messages 必须是正整数")
        elif self.recent_messages is not None:
            raise ValueError("当前上下文策略不接受 recent_messages")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("body 必须是非空字符串")
        if self.source not in ("builtin", "user", "project"):
            raise ValueError("source 无效")
        for field_name, value in (
            ("source_root", self.source_root),
            ("source_path", self.source_path),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{field_name} 必须是绝对 Path")
        if self.skill_directory is not None and (
            not isinstance(self.skill_directory, Path)
            or not self.skill_directory.is_absolute()
        ):
            raise ValueError("skill_directory 必须是绝对 Path 或 None")
        if not isinstance(self.dedicated_tools, tuple) or any(
            not isinstance(item, SkillToolDefinition)
            for item in self.dedicated_tools
        ):
            raise ValueError("dedicated_tools 无效")


@dataclass(frozen=True, slots=True)
class SkillCatalogSnapshot:
    definitions: tuple[SkillDefinition, ...]
    diagnostics: tuple[SkillDiagnostic, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.definitions, tuple) or any(
            not isinstance(item, SkillDefinition) for item in self.definitions
        ):
            raise ValueError("definitions 无效")
        names = tuple(item.name for item in self.definitions)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("definitions 必须按唯一 name 排序")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, SkillDiagnostic) for item in self.diagnostics
        ):
            raise ValueError("diagnostics 无效")
