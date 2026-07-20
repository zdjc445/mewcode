"""Managed Git worktree lifecycle and fail-closed cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
import os
from pathlib import Path
from typing import Self

from mewcode_agent.worktrees.git import (
    GitRepositoryIdentity,
    GitRunner,
    read_linked_worktree_head,
)
from mewcode_agent.worktrees.initializer import WorktreeInitializer
from mewcode_agent.worktrees.models import (
    WorktreeCloseResult,
    WorktreeCreateResult,
    WorktreeError,
    WorktreeKind,
    WorktreeRecord,
    WorktreeRuntimeConfig,
    WorktreeState,
    WorktreeStatus,
    WorktreeSwitchResult,
    managed_worktree_path,
    validate_object_id,
    validate_task_id,
    validate_worktree_name,
    worktree_branch_name,
)
from mewcode_agent.worktrees.storage import (
    load_worktree_state,
    read_worktree_state_main_root,
    worktree_state_lock,
    write_worktree_state,
)


class WorktreeManager:
    def __init__(
        self,
        *,
        config: WorktreeRuntimeConfig,
        git: GitRunner | None,
        identity: GitRepositoryIdentity | None,
        state: WorktreeState | None,
        unavailable_error: WorktreeError | None,
        now: Callable[[], datetime],
    ) -> None:
        self._config = config
        self._git = git
        self._identity = identity
        self._state = state
        self._unavailable_error = unavailable_error
        self._now = now
        self._operation_lock = asyncio.Lock()
        self._active_owners: set[str] = set()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closed = False
        self._cleanup_diagnostics: dict[str, str] = {}
        if identity is not None and state is not None and git is not None:
            self._managed_root = (
                state.main_root / ".mewcode" / ".worktrees"
            ).resolve(strict=False)
            state_parent = identity.common_git_dir / "mewcode-agent"
            self._state_path = state_parent / "worktrees.json"
            self._lock_path = state_parent / "worktrees.lock"
            self._initializer = WorktreeInitializer(
                main_root=state.main_root,
                config=config,
                git=git,
            )
        else:
            self._managed_root = None
            self._state_path = None
            self._lock_path = None
            self._initializer = None

    @classmethod
    async def open(
        cls,
        startup_directory: Path,
        config: WorktreeRuntimeConfig,
        *,
        git: GitRunner | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> Self:
        clock = now or (lambda: datetime.now().astimezone())
        try:
            runner = git or GitRunner()
            identity = await runner.repository_identity(
                startup_directory.resolve(strict=True)
            )
            state_path = (
                identity.common_git_dir
                / "mewcode-agent"
                / "worktrees.json"
            )
            stored_main = read_worktree_state_main_root(state_path)
            main_root = stored_main or identity.main_root
            managed_root = (
                main_root / ".mewcode" / ".worktrees"
            ).resolve(strict=False)
            state = load_worktree_state(
                state_path,
                main_root=main_root,
                managed_root=managed_root,
            )
            roots = {state.main_root, *(item.path for item in state.records)}
            if identity.main_root not in roots:
                raise WorktreeError(
                    "worktree_repository_unavailable",
                    "当前 worktree 不属于受管仓库",
                )
        except WorktreeError as exc:
            return cls(
                config=config,
                git=None,
                identity=None,
                state=None,
                unavailable_error=exc,
                now=clock,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            error = WorktreeError(
                "worktree_repository_unavailable",
                "无法初始化 Worktree 仓库",
            )
            error.__cause__ = exc
            return cls(
                config=config,
                git=None,
                identity=None,
                state=None,
                unavailable_error=error,
                now=clock,
            )
        return cls(
            config=config,
            git=runner,
            identity=identity,
            state=state,
            unavailable_error=None,
            now=clock,
        )

    @property
    def available(self) -> bool:
        return self._unavailable_error is None

    @property
    def main_root(self) -> Path | None:
        return None if self._state is None else self._state.main_root

    @property
    def managed_root(self) -> Path | None:
        return self._managed_root

    @property
    def active_name(self) -> str | None:
        return None if self._state is None else self._state.active_name

    def _require_available(
        self,
    ) -> tuple[GitRunner, GitRepositoryIdentity, Path, Path, Path]:
        if self._unavailable_error is not None:
            raise WorktreeError(
                self._unavailable_error.code,
                self._unavailable_error.message,
            )
        if (
            self._git is None
            or self._identity is None
            or self._managed_root is None
            or self._state_path is None
            or self._lock_path is None
        ):
            raise WorktreeError(
                "worktree_repository_unavailable",
                "Worktree manager 状态不完整",
            )
        return (
            self._git,
            self._identity,
            self._managed_root,
            self._state_path,
            self._lock_path,
        )

    def _current_time(self) -> datetime:
        current = self._now()
        if not isinstance(current, datetime) or current.utcoffset() is None:
            raise ValueError("Worktree clock 必须返回带 offset 的 datetime")
        return current

    def _load_state(self) -> WorktreeState:
        _, _, managed_root, state_path, _ = self._require_available()
        if self._state is None:
            raise WorktreeError(
                "worktree_repository_unavailable",
                "Worktree manager 状态不可用",
            )
        return load_worktree_state(
            state_path,
            main_root=self._state.main_root,
            managed_root=managed_root,
        )

    @staticmethod
    def _record(state: WorktreeState, name: str) -> WorktreeRecord:
        for record in state.records:
            if record.name == name:
                return record
        raise WorktreeError("worktree_not_found", "Worktree 不存在")

    @staticmethod
    def _replace_record(
        state: WorktreeState,
        record: WorktreeRecord,
    ) -> WorktreeState:
        records = tuple(
            sorted(
                (
                    record if item.name == record.name else item
                    for item in state.records
                ),
                key=lambda item: item.name,
            )
        )
        return WorktreeState(state.main_root, state.active_name, records)

    def list_records(self) -> tuple[WorktreeRecord, ...]:
        state = self._load_state()
        self._state = state
        return state.records

    async def create(
        self,
        name: str,
        *,
        kind: WorktreeKind = "manual",
        owner_id: str | None = None,
    ) -> WorktreeCreateResult:
        try:
            validate_worktree_name(name)
        except ValueError as exc:
            raise WorktreeError(
                "worktree_name_invalid",
                "Worktree 名称无效",
            ) from exc
        if kind == "manual":
            if owner_id is not None:
                raise WorktreeError(
                    "worktree_create_failed",
                    "manual Worktree 不能设置 owner",
                )
        elif kind == "worker":
            try:
                if owner_id is None:
                    raise ValueError("owner_id missing")
                validate_task_id(owner_id)
            except ValueError as exc:
                raise WorktreeError(
                    "worktree_create_failed",
                    "worker Worktree owner 无效",
                ) from exc
            if name != f"worker/{owner_id}":
                raise WorktreeError(
                    "worktree_create_failed",
                    "worker Worktree 名称与 owner 不匹配",
                )
        else:
            raise WorktreeError(
                "worktree_create_failed",
                "Worktree kind 无效",
            )
        git, identity, managed_root, state_path, lock_path = (
            self._require_available()
        )
        async with self._operation_lock:
            current = self._current_time()
            with worktree_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                existing = next(
                    (item for item in state.records if item.name == name),
                    None,
                )
                if existing is not None:
                    if existing.kind != kind or existing.owner_id != owner_id:
                        raise WorktreeError(
                            "worktree_already_exists",
                            "Worktree 已由不同类型或 owner 登记",
                        )
                    read_linked_worktree_head(
                        existing.path,
                        common_git_dir=identity.common_git_dir,
                        expected_branch=existing.branch,
                    )
                    created = datetime.fromisoformat(existing.created_at)
                    previous_used = datetime.fromisoformat(
                        existing.last_used_at
                    )
                    used = max(current, created, previous_used)
                    recovered = replace(
                        existing,
                        last_used_at=used.isoformat(),
                        expires_at=(
                            used + timedelta(hours=self._config.stale_after_hours)
                        ).isoformat(),
                    )
                    new_state = self._replace_record(state, recovered)
                    write_worktree_state(state_path, new_state)
                    self._state = new_state
                    return WorktreeCreateResult(recovered, True)
                path = managed_worktree_path(managed_root, name)
                if os.path.lexists(path):
                    raise WorktreeError(
                        "worktree_path_conflict",
                        "Worktree 目标路径已存在",
                    )
                branch = worktree_branch_name(name)
                branch_result = await git.run(
                    state.main_root,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                    check=False,
                    error_code="worktree_create_failed",
                )
                if branch_result.returncode == 0:
                    raise WorktreeError(
                        "worktree_branch_conflict",
                        "Worktree 分支已存在",
                    )
                if branch_result.returncode != 1:
                    raise WorktreeError(
                        "worktree_create_failed",
                        "无法检查 Worktree 分支",
                    )
                base_result = await git.run(
                    state.main_root,
                    "rev-parse",
                    "HEAD",
                    error_code="worktree_create_failed",
                )
                try:
                    base_head = validate_object_id(base_result.stdout)
                except ValueError as exc:
                    raise WorktreeError(
                        "worktree_create_failed",
                        "主仓库 HEAD 无效",
                    ) from exc
                self._ensure_exclude(identity.common_git_dir)
                added = False
                try:
                    await git.run(
                        state.main_root,
                        "worktree",
                        "add",
                        "-b",
                        branch,
                        str(path),
                        base_head,
                        timeout_seconds=120,
                        error_code="worktree_create_failed",
                    )
                    added = True
                    actual_head = read_linked_worktree_head(
                        path,
                        common_git_dir=identity.common_git_dir,
                        expected_branch=branch,
                    )
                    if actual_head != base_head:
                        raise WorktreeError(
                            "worktree_create_failed",
                            "新 Worktree HEAD 与基线不匹配",
                        )
                    if self._initializer is None:
                        raise WorktreeError(
                            "worktree_initialization_failed",
                            "Worktree initializer 不可用",
                        )
                    diagnostics = await self._initializer.initialize(path)
                    used = current
                    record = WorktreeRecord(
                        name=name,
                        path=path,
                        branch=branch,
                        base_head=base_head,
                        kind=kind,
                        owner_id=owner_id,
                        created_at=used.isoformat(),
                        last_used_at=used.isoformat(),
                        expires_at=(
                            used + timedelta(hours=self._config.stale_after_hours)
                        ).isoformat(),
                        initialization_diagnostics=diagnostics,
                    )
                    records = tuple(
                        sorted((*state.records, record), key=lambda item: item.name)
                    )
                    new_state = WorktreeState(
                        state.main_root,
                        state.active_name,
                        records,
                    )
                    write_worktree_state(state_path, new_state)
                except (Exception, asyncio.CancelledError):
                    if added:
                        await self._rollback_create(git, state.main_root, path, branch)
                    raise
                self._state = new_state
                return WorktreeCreateResult(record, False)

    @staticmethod
    def _ensure_exclude(common_git_dir: Path) -> None:
        path = common_git_dir / "info" / "exclude"
        line = "/.mewcode/.worktrees/"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_bytes() if path.exists() else b""
            decoded = existing.decode("utf-8", errors="strict")
            if line in decoded.splitlines():
                return
            with path.open("ab") as handle:
                if existing and not existing.endswith((b"\n", b"\r")):
                    handle.write(b"\n")
                handle.write(line.encode("utf-8") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, UnicodeError) as exc:
            raise WorktreeError(
                "worktree_create_failed",
                "无法更新 Git exclude",
            ) from exc

    @staticmethod
    async def _rollback_create(
        git: GitRunner,
        main_root: Path,
        path: Path,
        branch: str,
    ) -> None:
        try:
            await git.run(
                main_root,
                "worktree",
                "remove",
                "--force",
                str(path),
                timeout_seconds=120,
                error_code="worktree_create_failed",
            )
        except WorktreeError:
            return
        try:
            await git.run(
                main_root,
                "branch",
                "-D",
                branch,
                error_code="worktree_create_failed",
            )
        except WorktreeError:
            pass

    @staticmethod
    def _porcelain_entry_count(output: str) -> int:
        if not output:
            return 0
        entries = output.split("\x00")
        if entries[-1] != "":
            raise ValueError("porcelain output 未以 NUL 结束")
        entries.pop()
        count = 0
        index = 0
        while index < len(entries):
            entry = entries[index]
            if len(entry) < 3 or entry[2] != " ":
                raise ValueError("porcelain entry 无效")
            count += 1
            renamed = "R" in entry[:2] or "C" in entry[:2]
            index += 2 if renamed else 1
        if index != len(entries):
            raise ValueError("porcelain rename entry 无效")
        return count

    async def _status(self, record: WorktreeRecord) -> WorktreeStatus:
        git, _, managed_root, _, _ = self._require_available()
        expected_path = managed_worktree_path(managed_root, record.name)
        if record.path != expected_path or not record.path.is_dir():
            return WorktreeStatus(
                False,
                None,
                False,
                0,
                None,
                None,
                False,
                False,
                "worktree_status_failed",
            )
        try:
            porcelain = await git.run(
                record.path,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                error_code="worktree_status_failed",
            )
            dirty_count = self._porcelain_entry_count(porcelain.stdout)
            head_result = await git.run(
                record.path,
                "rev-parse",
                "HEAD",
                error_code="worktree_status_failed",
            )
            head = validate_object_id(head_result.stdout)
            upstream_result = await git.run(
                record.path,
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{upstream}",
                check=False,
                error_code="worktree_status_failed",
            )
            upstream: str | None = None
            unpushed_count: int | None = None
            if upstream_result.returncode == 0:
                if not upstream_result.stdout:
                    raise ValueError("upstream 为空")
                upstream = upstream_result.stdout
                count_result = await git.run(
                    record.path,
                    "rev-list",
                    "--count",
                    "@{upstream}..HEAD",
                    error_code="worktree_status_failed",
                )
                if not count_result.stdout.isdecimal():
                    raise ValueError("unpushed count 无效")
                unpushed_count = int(count_result.stdout)
                has_unpushed = unpushed_count > 0
            elif upstream_result.returncode == 128:
                has_unpushed = head != record.base_head
            else:
                raise ValueError("upstream 查询失败")
            dirty = dirty_count > 0
            return WorktreeStatus(
                True,
                head,
                dirty,
                dirty_count,
                upstream,
                unpushed_count,
                has_unpushed,
                not dirty and not has_unpushed,
                None,
            )
        except (WorktreeError, ValueError):
            return WorktreeStatus(
                True,
                None,
                False,
                0,
                None,
                None,
                False,
                False,
                "worktree_status_failed",
            )

    async def status(self, name: str) -> WorktreeStatus:
        try:
            validate_worktree_name(name)
        except ValueError as exc:
            raise WorktreeError(
                "worktree_name_invalid",
                "Worktree 名称无效",
            ) from exc
        async with self._operation_lock:
            state = self._load_state()
            self._state = state
            return await self._status(self._record(state, name))

    async def delete(
        self,
        name: str,
        *,
        discard_confirmed: bool = False,
    ) -> WorktreeStatus:
        if type(discard_confirmed) is not bool:
            raise ValueError("discard_confirmed 必须是 bool")
        try:
            validate_worktree_name(name)
        except ValueError as exc:
            raise WorktreeError(
                "worktree_name_invalid",
                "Worktree 名称无效",
            ) from exc
        git, identity, _, state_path, lock_path = self._require_available()
        async with self._operation_lock:
            current = self._current_time()
            with worktree_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                record = self._record(state, name)
                if state.active_name == name or (
                    record.owner_id is not None
                    and record.owner_id in self._active_owners
                ):
                    raise WorktreeError(
                        "worktree_in_use",
                        "Worktree 正在使用",
                    )
                try:
                    read_linked_worktree_head(
                        record.path,
                        common_git_dir=identity.common_git_dir,
                        expected_branch=record.branch,
                    )
                except WorktreeError as exc:
                    raise WorktreeError(
                        "worktree_delete_unsafe",
                        "无法验证 Worktree branch",
                    ) from exc
                status = await self._status(record)
                if status.reason_code is not None or not status.exists:
                    raise WorktreeError(
                        "worktree_delete_unsafe",
                        "无法确认 Worktree 删除安全性",
                    )
                if not discard_confirmed and not status.deletion_safe:
                    raise WorktreeError(
                        "worktree_delete_unsafe",
                        "Worktree 包含修改或未推送提交",
                    )
                arguments = ["worktree", "remove"]
                if discard_confirmed:
                    arguments.append("--force")
                arguments.append(str(record.path))
                await git.run(
                    state.main_root,
                    *arguments,
                    timeout_seconds=120,
                    error_code="worktree_remove_failed",
                )
                await git.run(
                    state.main_root,
                    "branch",
                    "-D" if discard_confirmed else "-d",
                    record.branch,
                    error_code="worktree_remove_failed",
                )
                records = tuple(
                    item for item in state.records if item.name != record.name
                )
                new_state = WorktreeState(
                    state.main_root,
                    state.active_name,
                    records,
                )
                write_worktree_state(state_path, new_state)
                self._state = new_state
                self._cleanup_diagnostics.pop(name, None)
                return status

    async def claim_owner(self, owner_id: str) -> None:
        validate_task_id(owner_id)
        async with self._operation_lock:
            self._active_owners.add(owner_id)

    async def release_owner(self, owner_id: str) -> None:
        async with self._operation_lock:
            self._active_owners.discard(owner_id)

    async def activate(self, name: str) -> WorktreeSwitchResult:
        try:
            validate_worktree_name(name)
        except ValueError as exc:
            raise WorktreeError(
                "worktree_name_invalid",
                "Worktree 名称无效",
            ) from exc
        _, identity, _, state_path, lock_path = self._require_available()
        async with self._operation_lock:
            current = self._current_time()
            with worktree_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                record = self._record(state, name)
                read_linked_worktree_head(
                    record.path,
                    common_git_dir=identity.common_git_dir,
                    expected_branch=record.branch,
                )
                if state.active_name == name:
                    self._state = state
                    return WorktreeSwitchResult(record.path, name, False)
                previous_used = datetime.fromisoformat(record.last_used_at)
                used = max(current, previous_used)
                updated = replace(
                    record,
                    last_used_at=used.isoformat(),
                    expires_at=(
                        used + timedelta(hours=self._config.stale_after_hours)
                    ).isoformat(),
                )
                records = tuple(
                    updated if item.name == name else item
                    for item in state.records
                )
                new_state = WorktreeState(state.main_root, name, records)
                write_worktree_state(state_path, new_state)
                self._state = new_state
                return WorktreeSwitchResult(record.path, name, True)

    async def deactivate(self) -> WorktreeSwitchResult:
        _, _, _, state_path, lock_path = self._require_available()
        async with self._operation_lock:
            current = self._current_time()
            with worktree_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                if state.active_name is None:
                    self._state = state
                    return WorktreeSwitchResult(
                        state.main_root,
                        None,
                        False,
                    )
                new_state = WorktreeState(state.main_root, None, state.records)
                write_worktree_state(state_path, new_state)
                self._state = new_state
                return WorktreeSwitchResult(
                    state.main_root,
                    None,
                    True,
                )

    def resume_target(self) -> Path:
        _, identity, _, _, _ = self._require_available()
        state = self._load_state()
        self._state = state
        if state.active_name is None:
            return state.main_root
        record = self._record(state, state.active_name)
        read_linked_worktree_head(
            record.path,
            common_git_dir=identity.common_git_dir,
            expected_branch=record.branch,
        )
        return record.path

    def start_cleanup(self) -> None:
        self._require_available()
        if self._closed:
            raise RuntimeError("WorktreeManager 已关闭")
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(),
                name="mewcode-worktree-cleanup",
            )

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.cleanup_interval_seconds)
                await self.cleanup_once()
        except asyncio.CancelledError:
            raise

    async def cleanup_once(self) -> None:
        current = self._current_time()
        state = self._load_state()
        for record in state.records:
            if record.kind != "worker":
                continue
            expires = datetime.fromisoformat(record.expires_at)
            if current < expires:
                continue
            if record.name == state.active_name or (
                record.owner_id is not None
                and record.owner_id in self._active_owners
            ):
                self._cleanup_diagnostics[record.name] = "worktree_in_use"
                continue
            try:
                await self.delete(record.name)
            except WorktreeError as exc:
                self._cleanup_diagnostics[record.name] = exc.code

    async def close(self) -> WorktreeCloseResult:
        if self._closed:
            return WorktreeCloseResult(False)
        self._closed = True
        task = self._cleanup_task
        cancelled = task is not None and not task.done()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._cleanup_task = None
        self._active_owners.clear()
        return WorktreeCloseResult(cancelled)
