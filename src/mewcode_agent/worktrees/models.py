"""Validated models for managed Git worktrees."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Literal, TypeAlias


WorktreeKind: TypeAlias = Literal["manual", "worker"]

_NAME_SEGMENT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_TASK_ID = re.compile(r"[0-9a-f]{32}\Z")
_WINDOWS_DEVICES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class WorktreeConfigError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class WorktreeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def validate_worktree_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 96
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("worktree name 无效")
    segments = value.split("/")
    if not 1 <= len(segments) <= 4:
        raise ValueError("worktree name 段数无效")
    for segment in segments:
        if (
            segment in ("", ".", "..")
            or _NAME_SEGMENT.fullmatch(segment) is None
            or segment.casefold() in _WINDOWS_DEVICES
        ):
            raise ValueError("worktree name 段无效")
    return value


def validate_relative_config_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or value.endswith("/")
    ):
        raise ValueError("worktree 配置路径无效")
    raw_parts = value.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError("worktree 配置路径无效")
    path = PurePosixPath(value)
    parts = path.parts
    if (
        path.is_absolute()
        or bool(PureWindowsPath(value).drive)
        or not parts
        or parts[0] == ".git"
        or parts[:2] == (".mewcode", ".worktrees")
    ):
        raise ValueError("worktree 配置路径无效")
    return value


def worktree_branch_name(name: str) -> str:
    validate_worktree_name(name)
    digest = sha256(name.encode("utf-8")).hexdigest()[:12]
    prefix = "mewcode-wt-"
    suffix = f"-{digest}"
    slug = name.replace("/", "-")
    slug = slug[: 120 - len(prefix) - len(suffix)]
    return f"{prefix}{slug}{suffix}"


def managed_worktree_path(managed_root: Path, name: str) -> Path:
    validate_worktree_name(name)
    if not isinstance(managed_root, Path) or not managed_root.is_absolute():
        raise ValueError("managed_root 必须是绝对 Path")
    root = managed_root.resolve(strict=False)
    candidate = root.joinpath(*name.split("/")).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("worktree path 越界") from exc
    return candidate


def validate_object_id(value: str) -> str:
    if not isinstance(value, str) or _OBJECT_ID.fullmatch(value) is None:
        raise ValueError("Git object ID 无效")
    return value


def _validate_time(value: str, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 无效")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} 无效") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含 UTC offset")
    return parsed


@dataclass(frozen=True, slots=True)
class WorktreeRuntimeConfig:
    stale_after_hours: int = 72
    cleanup_interval_seconds: int = 1800
    local_config_files: tuple[str, ...] = ("settings.local.json",)
    dependency_links: tuple[str, ...] = ()
    copy_ignored: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.stale_after_hours) is not int
            or not 1 <= self.stale_after_hours <= 8760
        ):
            raise ValueError("stale_after_hours 必须是 1..8760 的整数")
        if (
            type(self.cleanup_interval_seconds) is not int
            or not 60 <= self.cleanup_interval_seconds <= 86400
        ):
            raise ValueError(
                "cleanup_interval_seconds 必须是 60..86400 的整数"
            )
        for field_name in (
            "local_config_files",
            "dependency_links",
            "copy_ignored",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, tuple):
                raise ValueError(f"{field_name} 必须是 tuple")
            for value in values:
                validate_relative_config_path(value)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} 不能包含重复路径")


@dataclass(frozen=True, slots=True)
class WorktreeInitializationDiagnostic:
    stage: str
    path: str
    code: str

    def __post_init__(self) -> None:
        for field_name in ("stage", "path", "code"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or not value
                or any(char in value for char in "\r\n\x00")
            ):
                raise ValueError(f"{field_name} 无效")


@dataclass(frozen=True, slots=True)
class WorktreeRecord:
    name: str
    path: Path
    branch: str
    base_head: str
    kind: WorktreeKind
    owner_id: str | None
    created_at: str
    last_used_at: str
    expires_at: str
    initialization_diagnostics: tuple[
        WorktreeInitializationDiagnostic, ...
    ] = ()

    def __post_init__(self) -> None:
        validate_worktree_name(self.name)
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("worktree path 必须是绝对 Path")
        if self.path != self.path.resolve(strict=False):
            raise ValueError("worktree path 必须规范化")
        if self.branch != worktree_branch_name(self.name):
            raise ValueError("worktree branch 与 name 不匹配")
        validate_object_id(self.base_head)
        if self.kind not in ("manual", "worker"):
            raise ValueError("worktree kind 无效")
        if self.kind == "manual":
            if self.owner_id is not None:
                raise ValueError("manual worktree owner_id 必须是 null")
        elif (
            not isinstance(self.owner_id, str)
            or _TASK_ID.fullmatch(self.owner_id) is None
            or self.name != f"worker/{self.owner_id}"
        ):
            raise ValueError("worker worktree owner_id 无效")
        created = _validate_time(self.created_at, "created_at")
        used = _validate_time(self.last_used_at, "last_used_at")
        expires = _validate_time(self.expires_at, "expires_at")
        if not created <= used <= expires:
            raise ValueError("worktree 时间顺序无效")
        if not isinstance(self.initialization_diagnostics, tuple) or any(
            not isinstance(item, WorktreeInitializationDiagnostic)
            for item in self.initialization_diagnostics
        ):
            raise ValueError("initialization_diagnostics 无效")


@dataclass(frozen=True, slots=True)
class WorktreeState:
    main_root: Path
    active_name: str | None
    records: tuple[WorktreeRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.main_root, Path) or not self.main_root.is_absolute():
            raise ValueError("main_root 必须是绝对 Path")
        if self.main_root != self.main_root.resolve(strict=False):
            raise ValueError("main_root 必须规范化")
        if self.active_name is not None:
            validate_worktree_name(self.active_name)
        if not isinstance(self.records, tuple):
            raise ValueError("records 必须是 tuple")
        names = tuple(record.name for record in self.records)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("records 必须按唯一 name 排序")
        paths = tuple(record.path for record in self.records)
        branches = tuple(record.branch for record in self.records)
        if len(paths) != len(set(paths)) or len(branches) != len(set(branches)):
            raise ValueError("record path 或 branch 重复")
        if self.active_name is not None and self.active_name not in set(names):
            raise ValueError("active_name 不存在于 records")


@dataclass(frozen=True, slots=True)
class WorktreeStatus:
    exists: bool
    head: str | None
    dirty: bool
    dirty_entry_count: int
    upstream: str | None
    unpushed_commit_count: int | None
    has_unpushed: bool
    deletion_safe: bool
    reason_code: str | None

    def __post_init__(self) -> None:
        if type(self.exists) is not bool or type(self.dirty) is not bool:
            raise ValueError("status bool 字段无效")
        if type(self.has_unpushed) is not bool or type(self.deletion_safe) is not bool:
            raise ValueError("status bool 字段无效")
        if type(self.dirty_entry_count) is not int or self.dirty_entry_count < 0:
            raise ValueError("dirty_entry_count 无效")
        if self.head is not None:
            validate_object_id(self.head)
        if self.unpushed_commit_count is not None and (
            type(self.unpushed_commit_count) is not int
            or self.unpushed_commit_count < 0
        ):
            raise ValueError("unpushed_commit_count 无效")
        if self.reason_code is not None and (
            not isinstance(self.reason_code, str) or not self.reason_code
        ):
            raise ValueError("reason_code 无效")


@dataclass(frozen=True, slots=True)
class WorktreeCreateResult:
    record: WorktreeRecord
    recovered: bool


@dataclass(frozen=True, slots=True)
class WorktreeCloseResult:
    cleanup_task_cancelled: bool
