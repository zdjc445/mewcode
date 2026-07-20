"""Strict Team state, append-only mailbox/history, and process lock."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from mewcode_agent.models import ChatMessage
from mewcode_agent.teams.models import (
    TeamError,
    TeamMailboxMessage,
    TeamMemberRecord,
    TeamPersistentState,
    TeamRecord,
    TeamTaskRecord,
)


_STATE_KEYS = {"version", "main_root", "active_team_id", "teams"}
_TEAM_KEYS = {
    "team_id",
    "name",
    "state",
    "base_head",
    "integration_worktree_name",
    "lead_mailbox_cursor",
    "created_at",
    "updated_at",
    "members",
    "tasks",
    "merged_task_ids",
}
_MEMBER_KEYS = {
    "member_id",
    "name",
    "role",
    "backend",
    "state",
    "current_task_id",
    "mailbox_cursor",
    "created_at",
    "updated_at",
}
_TASK_KEYS = {
    "task_id",
    "title",
    "instructions",
    "status",
    "assignee",
    "dependencies",
    "created_at",
    "updated_at",
    "started_at",
    "ended_at",
    "result",
    "error_code",
    "workspace_path",
    "workspace_preserved",
    "workspace_reason",
    "branch",
    "head",
    "integrated_head",
}
_MESSAGE_KEYS = {
    "version",
    "message_id",
    "team_id",
    "sender",
    "recipient",
    "kind",
    "created_at",
    "content",
}
_HISTORY_KEYS = {"version", "role", "content"}
_LOCK_KEYS = {"pid", "created_at"}
_LINE_LIMIT = 32 * 1024


def _state_error(message: str, cause: Exception | None = None) -> TeamError:
    error = TeamError("team_state_invalid", message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _mapping(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise _state_error(f"{label} 必须是字符串映射")
    result = cast(Mapping[str, Any], value)
    if set(result) != keys:
        raise _state_error(f"{label} 字段不完整或包含未知字段")
    return result


def _json_mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 包含重复字段")
        result[key] = value
    return result


def _loads(value: str, label: str) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_json_mapping)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _state_error(f"{label} 不是有效 JSON", exc)


def _task_data(task: TeamTaskRecord) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "instructions": task.instructions,
        "status": task.status,
        "assignee": task.assignee,
        "dependencies": list(task.dependencies),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "ended_at": task.ended_at,
        "result": task.result,
        "error_code": task.error_code,
        "workspace_path": (
            None if task.workspace_path is None else str(task.workspace_path)
        ),
        "workspace_preserved": task.workspace_preserved,
        "workspace_reason": task.workspace_reason,
        "branch": task.branch,
        "head": task.head,
        "integrated_head": task.integrated_head,
    }


def _member_data(member: TeamMemberRecord) -> dict[str, object]:
    return {
        "member_id": member.member_id,
        "name": member.name,
        "role": member.role,
        "backend": member.backend,
        "state": member.state,
        "current_task_id": member.current_task_id,
        "mailbox_cursor": member.mailbox_cursor,
        "created_at": member.created_at,
        "updated_at": member.updated_at,
    }


def _team_data(team: TeamRecord) -> dict[str, object]:
    return {
        "team_id": team.team_id,
        "name": team.name,
        "state": team.state,
        "base_head": team.base_head,
        "integration_worktree_name": team.integration_worktree_name,
        "lead_mailbox_cursor": team.lead_mailbox_cursor,
        "created_at": team.created_at,
        "updated_at": team.updated_at,
        "members": [_member_data(item) for item in team.members],
        "tasks": [_task_data(item) for item in team.tasks],
        "merged_task_ids": list(team.merged_task_ids),
    }


def team_state_data(state: TeamPersistentState) -> dict[str, object]:
    return {
        "version": 1,
        "main_root": str(state.main_root),
        "active_team_id": state.active_team_id,
        "teams": [_team_data(item) for item in state.teams],
    }


def _task(raw: Any) -> TeamTaskRecord:
    item = _mapping(raw, _TASK_KEYS, "Team task")
    dependencies = item["dependencies"]
    if not isinstance(dependencies, list):
        raise _state_error("Task dependencies 必须是列表")
    workspace = item["workspace_path"]
    return TeamTaskRecord(
        task_id=item["task_id"],
        title=item["title"],
        instructions=item["instructions"],
        status=item["status"],
        assignee=item["assignee"],
        dependencies=tuple(dependencies),
        created_at=item["created_at"],
        updated_at=item["updated_at"],
        started_at=item["started_at"],
        ended_at=item["ended_at"],
        result=item["result"],
        error_code=item["error_code"],
        workspace_path=None if workspace is None else Path(workspace),
        workspace_preserved=item["workspace_preserved"],
        workspace_reason=item["workspace_reason"],
        branch=item["branch"],
        head=item["head"],
        integrated_head=item["integrated_head"],
    )


def _member(raw: Any) -> TeamMemberRecord:
    item = _mapping(raw, _MEMBER_KEYS, "Team member")
    return TeamMemberRecord(**item)


def _team(raw: Any) -> TeamRecord:
    item = _mapping(raw, _TEAM_KEYS, "Team record")
    raw_members = item["members"]
    raw_tasks = item["tasks"]
    raw_merged = item["merged_task_ids"]
    if not isinstance(raw_members, list) or not isinstance(raw_tasks, list):
        raise _state_error("Team members/tasks 必须是列表")
    if not isinstance(raw_merged, list):
        raise _state_error("merged_task_ids 必须是列表")
    return TeamRecord(
        team_id=item["team_id"],
        name=item["name"],
        state=item["state"],
        base_head=item["base_head"],
        integration_worktree_name=item["integration_worktree_name"],
        lead_mailbox_cursor=item["lead_mailbox_cursor"],
        created_at=item["created_at"],
        updated_at=item["updated_at"],
        members=tuple(_member(value) for value in raw_members),
        tasks=tuple(_task(value) for value in raw_tasks),
        merged_task_ids=tuple(raw_merged),
    )


def load_team_state(path: Path, *, main_root: Path) -> TeamPersistentState:
    normalized_main = main_root.resolve(strict=True)
    if not path.exists():
        return TeamPersistentState(normalized_main, None, ())
    if not path.is_file():
        raise _state_error("Team 状态路径不是文件")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _state_error("无法读取 Team 状态", exc)
    raw = _loads(text, "Team 状态")
    data = _mapping(raw, _STATE_KEYS, "Team 状态")
    if type(data["version"]) is not int or data["version"] != 1:
        raise _state_error("Team 状态 version 必须是整数 1")
    if data["main_root"] != str(normalized_main):
        raise _state_error("Team 状态 main_root 不匹配")
    raw_teams = data["teams"]
    if not isinstance(raw_teams, list):
        raise _state_error("Team 状态 teams 必须是列表")
    try:
        return TeamPersistentState(
            normalized_main,
            data["active_team_id"],
            tuple(_team(value) for value in raw_teams),
        )
    except TeamError:
        raise
    except (TypeError, ValueError) as exc:
        raise _state_error("Team 状态内容无效", exc)


def write_team_state(path: Path, state: TeamPersistentState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    payload = json.dumps(
        team_state_data(state),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise _state_error("无法写入 Team 状态", exc)


def _message_data(message: TeamMailboxMessage) -> dict[str, object]:
    return {
        "version": 1,
        "message_id": message.message_id,
        "team_id": message.team_id,
        "sender": message.sender,
        "recipient": message.recipient,
        "kind": message.kind,
        "created_at": message.created_at,
        "content": message.content,
    }


def _append_json_line(path: Path, data: Mapping[str, object], code: str) -> None:
    _append_json_lines(path, (data,), code)


def _append_json_lines(
    path: Path,
    items: tuple[Mapping[str, object], ...],
    code: str,
) -> None:
    encoded = tuple(
        json.dumps(
            item,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        for item in items
    )
    if any(len(payload) > _LINE_LIMIT for payload in encoded):
        raise TeamError(code, "Team JSONL 单行超过限制")
    payload = b"\n".join(encoded) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size:
            with path.open("rb") as check:
                check.seek(-1, os.SEEK_END)
                if check.read(1) != b"\n":
                    raise TeamError(code, "Team JSONL 存在不完整末行")
        with path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except TeamError:
        raise
    except OSError as exc:
        raise TeamError(code, "无法追加 Team JSONL") from exc


def append_mailbox_message(path: Path, message: TeamMailboxMessage) -> None:
    _append_json_line(path, _message_data(message), "team_mailbox_invalid")


def _complete_lines(path: Path, code: str) -> tuple[str, ...]:
    if not path.exists():
        return ()
    if not path.is_file():
        raise TeamError(code, "Team JSONL 路径不是文件")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TeamError(code, "无法读取 Team JSONL") from exc
    raw_lines = data.split(b"\n")
    if raw_lines[-1] != b"":
        raw_lines.pop()
    else:
        raw_lines.pop()
    lines: list[str] = []
    for raw in raw_lines:
        if not raw or raw.endswith(b"\r") or len(raw) > _LINE_LIMIT:
            raise TeamError(code, "Team JSONL 包含无效行")
        try:
            lines.append(raw.decode("utf-8", errors="strict"))
        except UnicodeError as exc:
            raise TeamError(code, "Team JSONL 不是有效 UTF-8") from exc
    return tuple(lines)


def load_mailbox(path: Path) -> tuple[TeamMailboxMessage, ...]:
    messages: list[TeamMailboxMessage] = []
    for line in _complete_lines(path, "team_mailbox_invalid"):
        try:
            item = _mapping(
                _loads(line, "Mailbox line"),
                _MESSAGE_KEYS,
                "Mailbox line",
            )
            if type(item["version"]) is not int or item["version"] != 1:
                raise ValueError("version")
            values = dict(item)
            values.pop("version")
            messages.append(TeamMailboxMessage(**values))
        except TeamError as exc:
            raise TeamError("team_mailbox_invalid", "Mailbox line 无效") from exc
        except (TypeError, ValueError) as exc:
            raise TeamError("team_mailbox_invalid", "Mailbox line 无效") from exc
    ids = tuple(item.message_id for item in messages)
    if len(ids) != len(set(ids)):
        raise TeamError("team_mailbox_invalid", "Mailbox message_id 重复")
    return tuple(messages)


def append_member_history(
    path: Path,
    user_content: str,
    assistant_content: str,
) -> None:
    messages = (
        ChatMessage("user", user_content),
        ChatMessage("assistant", assistant_content),
    )
    _append_json_lines(
        path,
        tuple(
            {"version": 1, "role": message.role, "content": message.content}
            for message in messages
        ),
        "team_state_invalid",
    )


def load_member_history(path: Path, *, limit: int) -> tuple[ChatMessage, ...]:
    if type(limit) is not int or limit < 2 or limit % 2 != 0:
        raise ValueError("history limit 必须是大于等于 2 的偶数")
    result: list[ChatMessage] = []
    for index, line in enumerate(_complete_lines(path, "team_state_invalid")):
        try:
            item = _mapping(
                _loads(line, "Member history line"),
                _HISTORY_KEYS,
                "Member history line",
            )
            if type(item["version"]) is not int or item["version"] != 1:
                raise ValueError("version")
            expected = "user" if index % 2 == 0 else "assistant"
            if item["role"] != expected:
                raise ValueError("role")
            result.append(ChatMessage(item["role"], item["content"]))
        except TeamError:
            raise
        except (TypeError, ValueError) as exc:
            raise TeamError(
                "team_state_invalid",
                "Member history line 无效",
            ) from exc
    if len(result) % 2 != 0:
        result.pop()
    return tuple(result[-limit:])


def _pid_alive(pid: int) -> bool | None:
    if type(pid) is not int or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


@contextmanager
def team_state_lock(
    path: Path,
    *,
    now: Callable[[], datetime],
    stale_seconds: int = 300,
) -> Iterator[None]:
    current = now()
    if current.utcoffset() is None:
        raise ValueError("Team lock 时间必须包含 UTC offset")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "created_at": current.isoformat()},
        separators=(",", ":"),
    ).encode("utf-8")
    acquired = False
    for _ in range(2):
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            try:
                raw = _loads(path.read_text(encoding="utf-8"), "Team lock")
                data = _mapping(raw, _LOCK_KEYS, "Team lock")
                created = datetime.fromisoformat(data["created_at"])
                if created.utcoffset() is None:
                    raise ValueError
                expired = current - created > timedelta(seconds=stale_seconds)
                alive = _pid_alive(data["pid"])
            except Exception as exc:
                raise TeamError(
                    "team_state_locked",
                    "Team 状态锁无法确认",
                ) from exc
            if not expired or alive is not False:
                raise TeamError(
                    "team_state_locked",
                    "Team 状态正由其他进程使用",
                )
            try:
                path.unlink()
            except OSError as exc:
                raise TeamError(
                    "team_state_locked",
                    "无法回收过期 Team 状态锁",
                ) from exc
            continue
        except OSError as exc:
            raise TeamError("team_state_locked", "无法创建 Team 状态锁") from exc
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except OSError as exc:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise TeamError("team_state_locked", "无法写入 Team 状态锁") from exc
        finally:
            os.close(descriptor)
        acquired = True
        break
    if not acquired:
        raise TeamError("team_state_locked", "无法获取 Team 状态锁")
    try:
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
