"""Validated role and runtime configuration models for subworkers."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Literal, TypeAlias


WorkerSource: TypeAlias = Literal["plugin", "builtin", "user", "project"]
WorkerPermissionMode: TypeAlias = Literal[
    "inherit",
    "strict",
    "default",
    "permissive",
]
WorkerIsolation: TypeAlias = Literal["none", "worktree"]

WORKER_NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
WORKER_TOOL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class WorkerConfigError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("code 必须是非空字符串")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message 必须是非空字符串")
        self.code = code
        self.message = message
        super().__init__(message)


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("code 必须是非空字符串")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message 必须是非空字符串")
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WorkerDiagnostic:
    source: WorkerSource
    candidate: str
    code: str
    message: str

    def __post_init__(self) -> None:
        if self.source not in ("plugin", "builtin", "user", "project"):
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
class WorkerRoleDefinition:
    name: str
    description: str
    allowed_tools: tuple[str, ...] | None
    denied_tools: tuple[str, ...]
    model: str
    max_rounds: int
    permission_mode: WorkerPermissionMode
    isolation: WorkerIsolation
    body: str
    source: WorkerSource
    source_root: Path
    source_path: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or WORKER_NAME_PATTERN.fullmatch(self.name) is None
        ):
            raise ValueError("name 格式无效")
        if (
            not isinstance(self.description, str)
            or not self.description.strip()
            or any(char in self.description for char in "\r\n\x00")
        ):
            raise ValueError("description 必须是非空单行字符串")
        if self.allowed_tools is not None:
            self._validate_tools(self.allowed_tools, "allowed_tools")
        self._validate_tools(self.denied_tools, "denied_tools")
        if self.allowed_tools is not None and set(self.allowed_tools).intersection(
            self.denied_tools
        ):
            raise ValueError("allowed_tools 与 denied_tools 不能重叠")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model 必须是非空字符串")
        if type(self.max_rounds) is not int or not 1 <= self.max_rounds <= 30:
            raise ValueError("max_rounds 必须是 1..30 的整数")
        if self.permission_mode not in (
            "inherit",
            "strict",
            "default",
            "permissive",
        ):
            raise ValueError("permission_mode 无效")
        if self.isolation not in ("none", "worktree"):
            raise ValueError("isolation 无效")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("body 必须是非空字符串")
        if self.source not in ("plugin", "builtin", "user", "project"):
            raise ValueError("source 无效")
        for field_name, value in (
            ("source_root", self.source_root),
            ("source_path", self.source_path),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{field_name} 必须是绝对 Path")

    @staticmethod
    def _validate_tools(tools: tuple[str, ...], field_name: str) -> None:
        if not isinstance(tools, tuple) or any(
            not isinstance(name, str)
            or WORKER_TOOL_NAME_PATTERN.fullmatch(name) is None
            for name in tools
        ):
            raise ValueError(f"{field_name} 无效")
        if len(tools) != len(set(tools)):
            raise ValueError(f"{field_name} 不能包含重复名称")


@dataclass(frozen=True, slots=True)
class WorkerRuntimeConfig:
    max_concurrency: int = 4
    foreground_timeout_seconds: float = 15.0
    background_allowed_tools: tuple[str, ...] = (
        "read_file",
        "find_files",
        "search_code",
        "read_context_artifact",
    )
    enable_verify_role: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.max_concurrency) is not int
            or not 1 <= self.max_concurrency <= 16
        ):
            raise ValueError("max_concurrency 必须是 1..16 的整数")
        if (
            isinstance(self.foreground_timeout_seconds, bool)
            or not isinstance(self.foreground_timeout_seconds, (int, float))
            or not math.isfinite(float(self.foreground_timeout_seconds))
            or not 0 < float(self.foreground_timeout_seconds) <= 300
        ):
            raise ValueError(
                "foreground_timeout_seconds 必须大于 0 且不超过 300"
            )
        object.__setattr__(
            self,
            "foreground_timeout_seconds",
            float(self.foreground_timeout_seconds),
        )
        WorkerRoleDefinition._validate_tools(
            self.background_allowed_tools,
            "background_allowed_tools",
        )
        if not self.background_allowed_tools:
            raise ValueError("background_allowed_tools 不能为空")
        if "spawn_worker" in self.background_allowed_tools:
            raise ValueError("background_allowed_tools 不能包含 spawn_worker")
        if type(self.enable_verify_role) is not bool:
            raise ValueError("enable_verify_role 必须是 bool")


@dataclass(frozen=True, slots=True)
class WorkerCatalogSnapshot:
    definitions: tuple[WorkerRoleDefinition, ...]
    diagnostics: tuple[WorkerDiagnostic, ...]
    runtime_config: WorkerRuntimeConfig

    def __post_init__(self) -> None:
        if not isinstance(self.definitions, tuple) or any(
            not isinstance(item, WorkerRoleDefinition)
            for item in self.definitions
        ):
            raise ValueError("definitions 无效")
        names = tuple(item.name for item in self.definitions)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("definitions 必须按唯一 name 排序")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, WorkerDiagnostic)
            for item in self.diagnostics
        ):
            raise ValueError("diagnostics 无效")
        if not isinstance(self.runtime_config, WorkerRuntimeConfig):
            raise ValueError("runtime_config 无效")
