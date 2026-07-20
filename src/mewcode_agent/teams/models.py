"""Validated persistent team collaboration models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Literal, TypeAlias

from mewcode_agent.models import ChatMessage
from mewcode_agent.worktrees import validate_object_id


TeamStateName: TypeAlias = Literal["active", "paused", "closed", "merged"]
TeamMemberState: TypeAlias = Literal["idle", "running", "offline"]
TeamTaskStatus: TypeAlias = Literal[
    "blocked",
    "pending",
    "running",
    "completed",
    "integrated",
    "failed",
    "cancelled",
]
TeamMessageKind: TypeAlias = Literal[
    "message",
    "assignment",
    "result",
    "system",
]

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_TEAM_ID = re.compile(r"t[0-9a-f]{31}\Z")
_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]*\Z")
_RESERVED_MEMBER_NAMES = frozenset({"lead", "integration", "system"})


class TeamConfigError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class TeamError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def validate_team_name(value: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise ValueError("Team name 无效")
    return value


def validate_member_name(value: str) -> str:
    validate_team_name(value)
    if value in _RESERVED_MEMBER_NAMES:
        raise ValueError("Member name 使用保留名称")
    return value


def validate_team_id(value: str) -> str:
    if not isinstance(value, str) or _TEAM_ID.fullmatch(value) is None:
        raise ValueError("team_id 无效")
    return value


def validate_team_hex_id(value: str, field_name: str = "ID") -> str:
    if not isinstance(value, str) or _HEX_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} 无效")
    return value


def validate_team_error_code(value: str) -> str:
    if not isinstance(value, str) or _ERROR_CODE.fullmatch(value) is None:
        raise ValueError("Team error code 无效")
    return value


def _time(value: str, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 无效")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} 无效") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含 UTC offset")
    return parsed


def _single_line(value: str, *, maximum: int, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(char in value for char in "\r\n\x00")
    ):
        raise ValueError(f"{field_name} 无效")
    return value


@dataclass(frozen=True, slots=True)
class TeamRuntimeConfig:
    max_teams: int = 8
    max_members_per_team: int = 8
    max_tasks_per_team: int = 256
    scheduler_interval_seconds: int = 1
    member_timeout_seconds: int = 900
    member_history_messages: int = 40

    def __post_init__(self) -> None:
        for field_name, lower, upper in (
            ("max_teams", 1, 32),
            ("max_members_per_team", 1, 16),
            ("max_tasks_per_team", 1, 4096),
            ("scheduler_interval_seconds", 1, 60),
            ("member_timeout_seconds", 30, 86400),
            ("member_history_messages", 2, 200),
        ):
            value = getattr(self, field_name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{field_name} 超出范围")
        if self.member_history_messages % 2 != 0:
            raise ValueError("member_history_messages 必须是偶数")


@dataclass(frozen=True, slots=True)
class TeamMemberRecord:
    member_id: str
    name: str
    role: str
    backend: Literal["in_process"]
    state: TeamMemberState
    current_task_id: str | None
    mailbox_cursor: int
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        validate_team_hex_id(self.member_id, "member_id")
        validate_member_name(self.name)
        validate_team_name(self.role)
        if self.backend != "in_process":
            raise ValueError("Team backend 无效")
        if self.state not in ("idle", "running", "offline"):
            raise ValueError("Member state 无效")
        if self.state == "running":
            validate_team_hex_id(self.current_task_id, "current_task_id")
        elif self.current_task_id is not None:
            raise ValueError("非 running Member 不能有 current_task_id")
        if type(self.mailbox_cursor) is not int or self.mailbox_cursor < 0:
            raise ValueError("mailbox_cursor 无效")
        created = _time(self.created_at, "created_at")
        updated = _time(self.updated_at, "updated_at")
        if created > updated:
            raise ValueError("Member 时间顺序无效")


@dataclass(frozen=True, slots=True)
class TeamTaskRecord:
    task_id: str
    title: str
    instructions: str
    status: TeamTaskStatus
    assignee: str | None
    dependencies: tuple[str, ...]
    created_at: str
    updated_at: str
    started_at: str | None = None
    ended_at: str | None = None
    result: str | None = None
    error_code: str | None = None
    workspace_path: Path | None = None
    workspace_preserved: bool | None = None
    workspace_reason: str | None = None
    branch: str | None = None
    head: str | None = None
    integrated_head: str | None = None

    def __post_init__(self) -> None:
        validate_team_hex_id(self.task_id, "task_id")
        _single_line(self.title, maximum=200, field_name="title")
        if (
            not isinstance(self.instructions, str)
            or not self.instructions.strip()
            or len(self.instructions) > 32768
            or "\x00" in self.instructions
        ):
            raise ValueError("instructions 无效")
        if self.status not in (
            "blocked",
            "pending",
            "running",
            "completed",
            "integrated",
            "failed",
            "cancelled",
        ):
            raise ValueError("Task status 无效")
        if self.assignee is not None:
            validate_member_name(self.assignee)
        if (
            not isinstance(self.dependencies, tuple)
            or len(self.dependencies) > 32
            or len(self.dependencies) != len(set(self.dependencies))
        ):
            raise ValueError("dependencies 无效")
        for dependency in self.dependencies:
            validate_team_hex_id(dependency, "dependency")
        if self.task_id in self.dependencies:
            raise ValueError("Task 不能依赖自身")
        created = _time(self.created_at, "created_at")
        updated = _time(self.updated_at, "updated_at")
        if created > updated:
            raise ValueError("Task 时间顺序无效")
        started = (
            None if self.started_at is None else _time(self.started_at, "started_at")
        )
        ended = None if self.ended_at is None else _time(self.ended_at, "ended_at")
        if started is not None and (started < created or started > updated):
            raise ValueError("started_at 时间顺序无效")
        if ended is not None:
            lower_bound = created if started is None else started
            if ended < lower_bound or ended > updated:
                raise ValueError("ended_at 时间顺序无效")
        if self.status in ("blocked", "pending"):
            if any(
                value is not None
                for value in (
                    self.started_at,
                    self.ended_at,
                    self.result,
                    self.error_code,
                )
            ):
                raise ValueError("未启动 Task 包含终态字段")
        elif self.status == "running":
            if started is None or any(
                value is not None
                for value in (self.ended_at, self.result, self.error_code)
            ):
                raise ValueError("running Task 字段无效")
        elif self.status in ("completed", "integrated"):
            if (
                started is None
                or ended is None
                or not isinstance(self.result, str)
                or not self.result.strip()
                or len(self.result) > 8000
                or self.error_code is not None
            ):
                raise ValueError("成功 Task 字段无效")
            if self.workspace_path is None:
                raise ValueError("成功 Task 缺少 workspace")
        elif self.status == "failed":
            if started is None or ended is None or self.result is not None:
                raise ValueError("失败 Task 时间或 result 无效")
            if self.error_code is None:
                raise ValueError("失败 Task 缺少 error_code")
            validate_team_error_code(self.error_code)
        else:
            if ended is None or self.result is not None:
                raise ValueError("取消 Task 时间或 result 无效")
            if self.error_code is None:
                raise ValueError("取消 Task 缺少 error_code")
            validate_team_error_code(self.error_code)
        if self.workspace_path is None:
            if any(
                value is not None
                for value in (
                    self.workspace_preserved,
                    self.workspace_reason,
                    self.branch,
                    self.head,
                )
            ):
                raise ValueError("无 workspace 的 Task 包含 workspace 字段")
        else:
            if (
                not isinstance(self.workspace_path, Path)
                or not self.workspace_path.is_absolute()
                or self.workspace_path != self.workspace_path.resolve(strict=False)
                or type(self.workspace_preserved) is not bool
                or not isinstance(self.branch, str)
                or not self.branch
            ):
                raise ValueError("Task workspace 字段无效")
            if self.workspace_preserved:
                if not isinstance(self.workspace_reason, str) or not self.workspace_reason:
                    raise ValueError("保留 workspace 缺少 reason")
            elif self.workspace_reason is not None:
                raise ValueError("未保留 workspace 不能有 reason")
            if self.head is not None:
                validate_object_id(self.head)
        if self.status == "integrated":
            if self.integrated_head is None:
                raise ValueError("integrated Task 缺少 integrated_head")
            validate_object_id(self.integrated_head)
        elif self.integrated_head is not None:
            raise ValueError("非 integrated Task 不能有 integrated_head")


@dataclass(frozen=True, slots=True)
class TeamRecord:
    team_id: str
    name: str
    state: TeamStateName
    base_head: str
    integration_worktree_name: str
    lead_mailbox_cursor: int
    created_at: str
    updated_at: str
    members: tuple[TeamMemberRecord, ...]
    tasks: tuple[TeamTaskRecord, ...]
    merged_task_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_team_id(self.team_id)
        validate_team_name(self.name)
        if self.state not in ("active", "paused", "closed", "merged"):
            raise ValueError("Team state 无效")
        validate_object_id(self.base_head)
        if self.integration_worktree_name != f"team/{self.team_id}/integration":
            raise ValueError("integration_worktree_name 无效")
        if type(self.lead_mailbox_cursor) is not int or self.lead_mailbox_cursor < 0:
            raise ValueError("lead_mailbox_cursor 无效")
        created = _time(self.created_at, "created_at")
        updated = _time(self.updated_at, "updated_at")
        if created > updated:
            raise ValueError("Team 时间顺序无效")
        if not isinstance(self.members, tuple) or not self.members:
            raise ValueError("Team members 无效")
        member_ids = tuple(item.member_id for item in self.members)
        member_names = tuple(item.name for item in self.members)
        if (
            member_names != tuple(sorted(member_names))
            or len(member_ids) != len(set(member_ids))
            or len(member_names) != len(set(member_names))
        ):
            raise ValueError("Team members 必须按唯一 name 排序")
        if not isinstance(self.tasks, tuple):
            raise ValueError("Team tasks 无效")
        task_ids = tuple(item.task_id for item in self.tasks)
        if task_ids != tuple(sorted(task_ids)) or len(task_ids) != len(set(task_ids)):
            raise ValueError("Team tasks 必须按唯一 task_id 排序")
        known_members = set(member_names)
        known_tasks = set(task_ids)
        for member in self.members:
            if member.current_task_id is not None and member.current_task_id not in known_tasks:
                raise ValueError("Member current_task_id 不存在")
        for task in self.tasks:
            if task.assignee is not None and task.assignee not in known_members:
                raise ValueError("Task assignee 不存在")
            if not set(task.dependencies).issubset(known_tasks):
                raise ValueError("Task dependency 不存在")
        task_by_id = {item.task_id: item for item in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("Task dependencies 包含环")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in task_by_id[task_id].dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        for task in self.tasks:
            if task.status not in ("blocked", "pending"):
                continue
            ready = all(
                task_by_id[dependency].status in ("completed", "integrated")
                for dependency in task.dependencies
            )
            if ready != (task.status == "pending"):
                raise ValueError("Task blocked/pending 与 dependencies 不一致")
        running_by_member: dict[str, str] = {}
        for task in self.tasks:
            if task.status != "running":
                continue
            if task.assignee is None or task.assignee in running_by_member:
                raise ValueError("running Task assignee 无效或重复")
            running_by_member[task.assignee] = task.task_id
        for member in self.members:
            expected = running_by_member.get(member.name)
            if member.state == "running":
                if member.current_task_id != expected:
                    raise ValueError("Member 与 running Task 不一致")
            elif expected is not None:
                raise ValueError("running Task 没有 running Member")
        if (
            not isinstance(self.merged_task_ids, tuple)
            or tuple(sorted(self.merged_task_ids)) != self.merged_task_ids
            or len(self.merged_task_ids) != len(set(self.merged_task_ids))
            or not set(self.merged_task_ids).issubset(known_tasks)
        ):
            raise ValueError("merged_task_ids 无效")
        integrated = {
            item.task_id for item in self.tasks if item.status == "integrated"
        }
        if set(self.merged_task_ids) != integrated:
            raise ValueError("merged_task_ids 与 Task 状态不一致")


@dataclass(frozen=True, slots=True)
class TeamPersistentState:
    main_root: Path
    active_team_id: str | None
    teams: tuple[TeamRecord, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.main_root, Path)
            or not self.main_root.is_absolute()
            or self.main_root != self.main_root.resolve(strict=False)
        ):
            raise ValueError("Team main_root 无效")
        if not isinstance(self.teams, tuple):
            raise ValueError("teams 无效")
        ids = tuple(item.team_id for item in self.teams)
        names = tuple(item.name for item in self.teams)
        if (
            ids != tuple(sorted(ids))
            or len(ids) != len(set(ids))
            or len(names) != len(set(names))
        ):
            raise ValueError("teams 必须按唯一 team_id 排序")
        if self.active_team_id is not None:
            validate_team_id(self.active_team_id)
            active = next(
                (item for item in self.teams if item.team_id == self.active_team_id),
                None,
            )
            if active is None or active.state not in ("active", "paused"):
                raise ValueError("active_team_id 无效")
        active_ids = tuple(
            item.team_id
            for item in self.teams
            if item.state in ("active", "paused")
        )
        expected_active = (
            () if self.active_team_id is None else (self.active_team_id,)
        )
        if active_ids != expected_active:
            raise ValueError("active/paused Team 与 active_team_id 不一致")


@dataclass(frozen=True, slots=True)
class TeamMailboxMessage:
    message_id: str
    team_id: str
    sender: str
    recipient: str
    kind: TeamMessageKind
    created_at: str
    content: str

    def __post_init__(self) -> None:
        validate_team_hex_id(self.message_id, "message_id")
        validate_team_id(self.team_id)
        if self.sender not in ("lead", "system"):
            validate_member_name(self.sender)
        if self.recipient != "lead":
            validate_member_name(self.recipient)
        if self.kind not in ("message", "assignment", "result", "system"):
            raise ValueError("Message kind 无效")
        _time(self.created_at, "created_at")
        if (
            not isinstance(self.content, str)
            or not self.content.strip()
            or len(self.content) > 8192
            or "\x00" in self.content
        ):
            raise ValueError("Message content 无效")


@dataclass(frozen=True, slots=True)
class TeamDependencyResult:
    task_id: str
    title: str
    status: Literal["completed", "integrated"]
    result: str

    def __post_init__(self) -> None:
        validate_team_hex_id(self.task_id, "dependency task_id")
        _single_line(self.title, maximum=200, field_name="dependency title")
        if self.status not in ("completed", "integrated"):
            raise ValueError("dependency status 无效")
        if (
            not isinstance(self.result, str)
            or not self.result.strip()
            or len(self.result) > 8000
        ):
            raise ValueError("dependency result 无效")


@dataclass(frozen=True, slots=True)
class TeamBackendRequest:
    team_id: str
    member: TeamMemberRecord
    task: TeamTaskRecord
    dependencies: tuple[TeamDependencyResult, ...]
    mailbox: tuple[TeamMailboxMessage, ...]
    history: tuple[ChatMessage, ...]

    def __post_init__(self) -> None:
        validate_team_id(self.team_id)
        if self.member.state != "running":
            raise ValueError("Backend member 必须是 running")
        if self.task.status != "running":
            raise ValueError("Backend task 必须是 running")
        if self.member.current_task_id != self.task.task_id:
            raise ValueError("Backend member/task 不匹配")
        if self.task.assignee != self.member.name:
            raise ValueError("Backend task assignee 不匹配")
        if (
            not isinstance(self.dependencies, tuple)
            or tuple(item.task_id for item in self.dependencies)
            != self.task.dependencies
        ):
            raise ValueError("Backend dependencies 与 task 不匹配")
        if not isinstance(self.mailbox, tuple) or any(
            item.team_id != self.team_id or item.recipient != self.member.name
            for item in self.mailbox
        ):
            raise ValueError("Backend mailbox 无效")
        if not isinstance(self.history, tuple) or len(self.history) % 2 != 0:
            raise ValueError("Backend history 无效")
        for index, message in enumerate(self.history):
            expected = "user" if index % 2 == 0 else "assistant"
            if message.role != expected or message.tool_calls or message.thinking_blocks:
                raise ValueError("Backend history 必须是普通 user/assistant 对")


@dataclass(frozen=True, slots=True)
class TeamBackendResult:
    state: Literal["completed", "failed", "cancelled"]
    result: str | None
    error_code: str | None
    workspace_path: Path | None
    workspace_preserved: bool | None
    workspace_reason: str | None
    branch: str | None
    head: str | None

    def __post_init__(self) -> None:
        if self.state not in ("completed", "failed", "cancelled"):
            raise ValueError("Backend result state 无效")
        if self.state == "completed":
            if self.workspace_path is None:
                raise ValueError("Backend completed result 缺少 workspace")
            if (
                not isinstance(self.result, str)
                or not self.result.strip()
                or self.error_code is not None
            ):
                raise ValueError("Backend completed result 无效")
        else:
            if self.result is not None or self.error_code is None:
                raise ValueError("Backend failure result 无效")
            validate_team_error_code(self.error_code)
        if self.workspace_path is None:
            if any(
                value is not None
                for value in (
                    self.workspace_preserved,
                    self.workspace_reason,
                    self.branch,
                    self.head,
                )
            ):
                raise ValueError("Backend result 无 workspace 但包含 workspace 字段")
        elif (
            not isinstance(self.workspace_path, Path)
            or not self.workspace_path.is_absolute()
            or self.workspace_path != self.workspace_path.resolve(strict=False)
            or type(self.workspace_preserved) is not bool
            or not isinstance(self.branch, str)
            or not self.branch
        ):
            raise ValueError("Backend result workspace 无效")
        else:
            if self.workspace_preserved:
                if not isinstance(self.workspace_reason, str) or not self.workspace_reason:
                    raise ValueError("Backend preserved workspace 缺少 reason")
            elif self.workspace_reason is not None:
                raise ValueError("Backend clean workspace 不能有 reason")
            if self.head is not None:
                validate_object_id(self.head)


@dataclass(frozen=True, slots=True)
class TeamCloseResult:
    active_episodes: int
    cancelled_episodes: int
    persisted_episodes: int

    def __post_init__(self) -> None:
        for field_name in (
            "active_episodes",
            "cancelled_episodes",
            "persisted_episodes",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} 无效")
        if self.cancelled_episodes > self.active_episodes:
            raise ValueError("cancelled_episodes 超过 active_episodes")
        if self.persisted_episodes > self.active_episodes:
            raise ValueError("persisted_episodes 超过 active_episodes")


@dataclass(frozen=True, slots=True)
class TeamMainMergePreview:
    team_id: str
    team_name: str
    main_path: Path
    integration_path: Path
    main_head: str
    integration_head: str
    task_counts: tuple[tuple[TeamTaskStatus, int], ...]
    main_dirty: bool
    integration_dirty: bool

    def __post_init__(self) -> None:
        validate_team_id(self.team_id)
        validate_team_name(self.team_name)
        for field_name in ("main_path", "integration_path"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, Path)
                or not value.is_absolute()
                or value != value.resolve(strict=False)
            ):
                raise ValueError(f"{field_name} 无效")
        validate_object_id(self.main_head)
        validate_object_id(self.integration_head)
        valid_states = {
            "blocked",
            "pending",
            "running",
            "completed",
            "integrated",
            "failed",
            "cancelled",
        }
        statuses = tuple(item[0] for item in self.task_counts)
        if (
            not isinstance(self.task_counts, tuple)
            or statuses != tuple(sorted(statuses))
            or len(statuses) != len(set(statuses))
            or any(
                status not in valid_states or type(count) is not int or count < 0
                for status, count in self.task_counts
            )
        ):
            raise ValueError("task_counts 无效")
        if type(self.main_dirty) is not bool or type(self.integration_dirty) is not bool:
            raise ValueError("Merge preview dirty 字段无效")
