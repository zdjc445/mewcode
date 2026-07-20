"""Validated role and runtime configuration models for subworkers."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Literal, TypeAlias

from mewcode_agent.models import ChatMessage


WorkerSource: TypeAlias = Literal["plugin", "builtin", "user", "project"]
WorkerPermissionMode: TypeAlias = Literal[
    "inherit",
    "strict",
    "default",
    "permissive",
]
WorkerIsolation: TypeAlias = Literal["none", "worktree"]
WorkerKind: TypeAlias = Literal["definition", "fork", "hook"]
WorkerState: TypeAlias = Literal[
    "starting",
    "running",
    "completed",
    "failed",
    "cancelled",
]
WorkerMode: TypeAlias = Literal["foreground", "background"]
WorkerTransition: TypeAlias = Literal[
    "explicit",
    "fork_forced",
    "timeout",
    "escape",
]

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


@dataclass(frozen=True, slots=True)
class WorkerUsageSnapshot:
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0
    unavailable_rounds: int = 0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.prompt_tokens,
                self.cache_hit_tokens,
                self.cache_miss_tokens,
                self.completion_tokens,
                self.unavailable_rounds,
            )
        ):
            raise ValueError("Worker usage 字段必须是非负整数")

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "completion_tokens": self.completion_tokens,
            "unavailable_rounds": self.unavailable_rounds,
        }


@dataclass(frozen=True, slots=True)
class WorkerExecutionSpec:
    task_id: str
    session_id: str
    worker_type: str
    kind: WorkerKind
    task: str
    definition: WorkerRoleDefinition | None
    parent_history: tuple[ChatMessage, ...]
    visible_tools: frozenset[str]
    provider_id: str
    model: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{32}", self.task_id) is None:
            raise ValueError("task_id 必须是 32 位小写十六进制")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id 必须是非空字符串")
        if (
            not isinstance(self.worker_type, str)
            or (
                self.worker_type != "fork"
                and WORKER_NAME_PATTERN.fullmatch(self.worker_type) is None
            )
        ):
            raise ValueError("worker_type 无效")
        if self.kind not in ("definition", "fork", "hook"):
            raise ValueError("kind 无效")
        if (
            not isinstance(self.task, str)
            or not self.task.strip()
            or len(self.task) > 32768
        ):
            raise ValueError("task 必须是 1..32768 code points 的字符串")
        if self.kind == "definition" and self.definition is None:
            raise ValueError("definition kind 必须携带角色定义")
        if self.kind == "fork" and self.definition is not None:
            raise ValueError("fork kind 不能携带角色定义")
        if not isinstance(self.parent_history, tuple) or any(
            not isinstance(item, ChatMessage) for item in self.parent_history
        ):
            raise ValueError("parent_history 无效")
        if not isinstance(self.visible_tools, frozenset):
            raise ValueError("visible_tools 必须是 frozenset")
        if "spawn_worker" in self.visible_tools:
            raise ValueError("visible_tools 不能包含 spawn_worker")
        if not isinstance(self.provider_id, str) or not self.provider_id:
            raise ValueError("provider_id 必须是非空字符串")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model 必须是非空字符串")


@dataclass(frozen=True, slots=True)
class WorkerExecutionOutcome:
    result: str
    report_format_valid: bool

    def __post_init__(self) -> None:
        if not isinstance(self.result, str) or not self.result.strip():
            raise ValueError("Worker result 必须是非空字符串")
        if type(self.report_format_valid) is not bool:
            raise ValueError("report_format_valid 必须是 bool")


@dataclass(frozen=True, slots=True)
class WorkerTaskSnapshot:
    task_id: str
    session_id: str
    worker_type: str
    kind: WorkerKind
    state: WorkerState
    mode: WorkerMode
    transition: WorkerTransition | None
    task: str
    provider_id: str
    model: str
    visible_tools: tuple[str, ...]
    created_at: str
    started_at: str | None
    ended_at: str | None
    usage: WorkerUsageSnapshot
    result: str | None
    error_code: str | None
    report_format_valid: bool | None


@dataclass(frozen=True, slots=True)
class WorkerNotification:
    task_id: str
    worker_type: str
    status: Literal["completed", "failed", "cancelled"]
    usage: WorkerUsageSnapshot
    result: str
    error_code: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "worker_terminal",
            "task_id": self.task_id,
            "worker_type": self.worker_type,
            "status": self.status,
            "usage": self.usage.to_dict(),
            "result": self.result,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class WorkerCloseResult:
    active_tasks: int
    cancelled_tasks: int
    cleared_notifications: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.active_tasks,
                self.cancelled_tasks,
                self.cleared_notifications,
            )
        ):
            raise ValueError("Worker close 统计必须是非负整数")
