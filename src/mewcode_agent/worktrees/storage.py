"""Strict atomic JSON state storage and cross-process lock."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from mewcode_agent.worktrees.models import (
    WorktreeError,
    WorktreeInitializationDiagnostic,
    WorktreeRecord,
    WorktreeState,
    managed_worktree_path,
)


_STATE_KEYS = {"version", "main_root", "active_name", "records"}
_RECORD_KEYS = {
    "name",
    "path",
    "branch",
    "base_head",
    "kind",
    "owner_id",
    "created_at",
    "last_used_at",
    "expires_at",
    "initialization_diagnostics",
}
_DIAGNOSTIC_KEYS = {"stage", "path", "code"}
_LOCK_KEYS = {"pid", "created_at"}


def _state_error(message: str, cause: Exception | None = None) -> WorktreeError:
    error = WorktreeError("worktree_state_invalid", message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _exact_mapping(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise _state_error(f"{label} 必须是字符串映射")
    data = cast(Mapping[str, Any], value)
    if set(data) != keys:
        raise _state_error(f"{label} 字段不完整或包含未知字段")
    return data


def _json_mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 包含重复字段")
        result[key] = value
    return result


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_mapping,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _state_error(f"无法读取 {label}", exc)


def read_worktree_state_main_root(path: Path) -> Path | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise _state_error("Worktree 状态路径不是文件")
    raw = _read_json(path, "Worktree 状态")
    data = _exact_mapping(raw, _STATE_KEYS, "Worktree 状态")
    if type(data["version"]) is not int or data["version"] != 1:
        raise _state_error("Worktree 状态 version 必须是整数 1")
    if not isinstance(data["main_root"], str):
        raise _state_error("Worktree 状态 main_root 无效")
    try:
        main_root = Path(data["main_root"])
        normalized = main_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _state_error("Worktree 状态 main_root 无效", exc)
    if (
        not main_root.is_absolute()
        or main_root != normalized
        or not normalized.is_dir()
    ):
        raise _state_error("Worktree 状态 main_root 无效")
    return normalized


def load_worktree_state(
    path: Path,
    *,
    main_root: Path,
    managed_root: Path,
) -> WorktreeState:
    normalized_main = main_root.resolve(strict=True)
    normalized_managed = managed_root.resolve(strict=False)
    if not path.exists():
        return WorktreeState(normalized_main, None, ())
    if not path.is_file():
        raise _state_error("Worktree 状态路径不是文件")
    try:
        raw = _read_json(path, "Worktree 状态")
    except WorktreeError:
        raise
    data = _exact_mapping(raw, _STATE_KEYS, "Worktree 状态")
    if type(data["version"]) is not int or data["version"] != 1:
        raise _state_error("Worktree 状态 version 必须是整数 1")
    if data["main_root"] != str(normalized_main):
        raise _state_error("Worktree 状态 main_root 不匹配")
    raw_records = data["records"]
    if not isinstance(raw_records, list):
        raise _state_error("Worktree 状态 records 必须是列表")
    records: list[WorktreeRecord] = []
    try:
        for raw_record in raw_records:
            item = _exact_mapping(raw_record, _RECORD_KEYS, "Worktree record")
            raw_diagnostics = item["initialization_diagnostics"]
            if not isinstance(raw_diagnostics, list):
                raise _state_error("initialization_diagnostics 必须是列表")
            diagnostics = tuple(
                WorktreeInitializationDiagnostic(
                    **_exact_mapping(raw_item, _DIAGNOSTIC_KEYS, "diagnostic")
                )
                for raw_item in raw_diagnostics
            )
            record = WorktreeRecord(
                item["name"],
                Path(item["path"]),
                item["branch"],
                item["base_head"],
                item["kind"],
                item["owner_id"],
                item["created_at"],
                item["last_used_at"],
                item["expires_at"],
                diagnostics,
            )
            if record.path != managed_worktree_path(
                normalized_managed, record.name
            ):
                raise _state_error("Worktree record path 与 name 不匹配")
            records.append(record)
        return WorktreeState(
            normalized_main,
            data["active_name"],
            tuple(records),
        )
    except WorktreeError:
        raise
    except (TypeError, ValueError) as exc:
        raise _state_error("Worktree 状态内容无效", exc)


def _diagnostic_data(item: WorktreeInitializationDiagnostic) -> dict[str, str]:
    return {"stage": item.stage, "path": item.path, "code": item.code}


def state_data(state: WorktreeState) -> dict[str, object]:
    return {
        "version": 1,
        "main_root": str(state.main_root),
        "active_name": state.active_name,
        "records": [
            {
                "name": item.name,
                "path": str(item.path),
                "branch": item.branch,
                "base_head": item.base_head,
                "kind": item.kind,
                "owner_id": item.owner_id,
                "created_at": item.created_at,
                "last_used_at": item.last_used_at,
                "expires_at": item.expires_at,
                "initialization_diagnostics": [
                    _diagnostic_data(diagnostic)
                    for diagnostic in item.initialization_diagnostics
                ],
            }
            for item in state.records
        ],
    }


def write_worktree_state(path: Path, state: WorktreeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    payload = json.dumps(
        state_data(state),
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
        raise _state_error("无法写入 Worktree 状态", exc)


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
def worktree_state_lock(
    path: Path,
    *,
    now: Callable[[], datetime],
    stale_seconds: int = 300,
) -> Iterator[None]:
    current = now()
    if current.utcoffset() is None:
        raise ValueError("lock 时间必须包含 UTC offset")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "created_at": current.isoformat()},
        separators=(",", ":"),
    )
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
                raw = json.loads(
                    path.read_text(encoding="utf-8"),
                    object_pairs_hook=_json_mapping,
                )
                data = _exact_mapping(raw, _LOCK_KEYS, "Worktree lock")
                created = datetime.fromisoformat(data["created_at"])
                if created.utcoffset() is None:
                    raise ValueError
                expired = current - created > timedelta(seconds=stale_seconds)
                alive = _pid_alive(data["pid"])
            except Exception as exc:
                raise WorktreeError(
                    "worktree_state_locked",
                    "Worktree 状态锁无法确认",
                ) from exc
            if not expired or alive is not False:
                raise WorktreeError(
                    "worktree_state_locked",
                    "Worktree 状态正由其他进程使用",
                )
            try:
                path.unlink()
            except OSError as exc:
                raise WorktreeError(
                    "worktree_state_locked",
                    "无法回收过期 Worktree 状态锁",
                ) from exc
            continue
        except OSError as exc:
            raise WorktreeError(
                "worktree_state_locked",
                "无法创建 Worktree 状态锁",
            ) from exc
        else:
            try:
                os.write(descriptor, payload.encode("utf-8"))
                os.fsync(descriptor)
            except OSError as exc:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise WorktreeError(
                    "worktree_state_locked",
                    "无法写入 Worktree 状态锁",
                ) from exc
            finally:
                os.close(descriptor)
            acquired = True
            break
    if not acquired:
        raise WorktreeError("worktree_state_locked", "无法获取 Worktree 状态锁")
    try:
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
