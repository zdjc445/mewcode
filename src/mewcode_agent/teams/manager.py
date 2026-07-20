"""Persistent Team lifecycle, deterministic DAG scheduling, and recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from typing import Self
from uuid import uuid4

from mewcode_agent.teams.backend import TeamBackend
from mewcode_agent.teams.models import (
    TeamBackendRequest,
    TeamBackendResult,
    TeamCloseResult,
    TeamDependencyResult,
    TeamError,
    TeamMailboxMessage,
    TeamMemberRecord,
    TeamPersistentState,
    TeamRecord,
    TeamRuntimeConfig,
    TeamTaskRecord,
    validate_member_name,
    validate_team_hex_id,
    validate_team_name,
)
from mewcode_agent.teams.storage import (
    append_mailbox_message,
    append_member_history,
    load_mailbox,
    load_member_history,
    load_team_state,
    team_state_lock,
    write_team_state,
)
from mewcode_agent.workers import WorkerCatalog
from mewcode_agent.worktrees import GitRunner, WorktreeError, WorktreeManager


_TERMINAL_TASK_STATES = frozenset(
    {"completed", "integrated", "failed", "cancelled"}
)


def _team_error(code: str, message: str, cause: Exception | None = None) -> TeamError:
    error = TeamError(code, message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _terminal_content(
    task: TeamTaskRecord,
    state: str,
    result: str | None,
    error_code: str | None,
) -> str:
    header = f"task_id={task.task_id}; title={task.title}; status={state}"
    content = header if result is None else f"{header}\n{result}"
    if error_code is not None:
        content = f"{header}; error_code={error_code}"
    if len(content) <= 8000:
        return content
    marker = "\n...[team notification truncated]...\n"
    return content[:5900] + marker + content[-2000:]


def _history_user_content(
    task: TeamTaskRecord,
    mailbox: tuple[TeamMailboxMessage, ...],
) -> str:
    serialized_mailbox = json.dumps(
        [
            {
                "message_id": item.message_id,
                "sender": item.sender,
                "kind": item.kind,
                "created_at": item.created_at,
                "content": item.content,
            }
            for item in mailbox
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"title:\n{task.title}\n\n"
        f"instructions:\n{task.instructions}\n\n"
        f"mailbox:\n{serialized_mailbox}"
    )


class TeamManager:
    def __init__(
        self,
        *,
        config: TeamRuntimeConfig,
        catalog: WorkerCatalog,
        backend: TeamBackend,
        worktree_manager: WorktreeManager,
        state: TeamPersistentState | None,
        state_path: Path | None,
        lock_path: Path | None,
        data_root: Path | None,
        unavailable_error: TeamError | None,
        now: Callable[[], datetime],
        id_factory: Callable[[], str],
    ) -> None:
        self._config = config
        self._catalog = catalog
        self._backend = backend
        self._worktree_manager = worktree_manager
        self._state = state
        self._state_path = state_path
        self._lock_path = lock_path
        self._data_root = data_root
        self._unavailable_error = unavailable_error
        self._now = now
        self._id_factory = id_factory
        self._operation_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._episodes: dict[str, asyncio.Task[bool]] = {}
        self._episode_ready: dict[str, asyncio.Event] = {}
        self._cancel_reasons: dict[str, str] = {}
        self._accepting = True
        self._close_result: TeamCloseResult | None = None

    @classmethod
    async def open(
        cls,
        startup_directory: Path,
        config: TeamRuntimeConfig,
        *,
        catalog: WorkerCatalog,
        backend: TeamBackend,
        worktree_manager: WorktreeManager,
        git: GitRunner | None = None,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> Self:
        clock = now or (lambda: datetime.now().astimezone())
        identifiers = id_factory or (lambda: uuid4().hex)
        try:
            if not worktree_manager.available or worktree_manager.main_root is None:
                raise TeamError(
                    "team_repository_unavailable",
                    "Team 需要可用的 Worktree manager",
                )
            runner = git or GitRunner()
            identity = await runner.repository_identity(
                startup_directory.resolve(strict=True)
            )
            state_parent = identity.common_git_dir / "mewcode-agent"
            state_path = state_parent / "teams.json"
            lock_path = state_parent / "teams.lock"
            data_root = state_parent / "teams"
            state = load_team_state(
                state_path,
                main_root=worktree_manager.main_root,
            )
            cls._validate_capacity(state, config)
            manager = cls(
                config=config,
                catalog=catalog,
                backend=backend,
                worktree_manager=worktree_manager,
                state=state,
                state_path=state_path,
                lock_path=lock_path,
                data_root=data_root,
                unavailable_error=None,
                now=clock,
                id_factory=identifiers,
            )
            await manager._recover_startup()
            return manager
        except TeamError as exc:
            error = exc
        except (WorktreeError, OSError, RuntimeError, ValueError) as exc:
            error = _team_error(
                "team_repository_unavailable",
                "无法初始化 Team 仓库",
                exc,
            )
        return cls(
            config=config,
            catalog=catalog,
            backend=backend,
            worktree_manager=worktree_manager,
            state=None,
            state_path=None,
            lock_path=None,
            data_root=None,
            unavailable_error=error,
            now=clock,
            id_factory=identifiers,
        )

    @staticmethod
    def _validate_capacity(
        state: TeamPersistentState,
        config: TeamRuntimeConfig,
    ) -> None:
        if len(state.teams) > config.max_teams:
            raise TeamError("team_state_invalid", "Team 状态超过 team 容量")
        if any(
            len(team.members) > config.max_members_per_team
            or len(team.tasks) > config.max_tasks_per_team
            for team in state.teams
        ):
            raise TeamError("team_state_invalid", "Team 状态超过成员或任务容量")

    @property
    def available(self) -> bool:
        return self._unavailable_error is None

    @property
    def active_team_id(self) -> str | None:
        return None if self._state is None else self._state.active_team_id

    def _require_available(self) -> tuple[Path, Path, Path, TeamPersistentState]:
        if self._unavailable_error is not None:
            raise TeamError(
                self._unavailable_error.code,
                self._unavailable_error.message,
            )
        if (
            self._state_path is None
            or self._lock_path is None
            or self._data_root is None
            or self._state is None
        ):
            raise TeamError(
                "team_repository_unavailable",
                "Team manager 状态不完整",
            )
        return self._state_path, self._lock_path, self._data_root, self._state

    def _require_accepting(self) -> None:
        if not self._accepting:
            raise TeamError("team_closed", "Team manager 已关闭")

    def _current_time(self) -> datetime:
        current = self._now()
        if not isinstance(current, datetime) or current.utcoffset() is None:
            raise ValueError("Team clock 必须返回带 offset 的 datetime")
        return current

    def _new_hex_id(self, field_name: str) -> str:
        value = self._id_factory()
        try:
            return validate_team_hex_id(value, field_name)
        except ValueError as exc:
            raise TeamError("team_state_invalid", "Team id factory 返回无效 ID") from exc

    def _new_team_id(self) -> str:
        return "t" + self._new_hex_id("team seed")[-31:]

    def _load_state(self) -> TeamPersistentState:
        state_path, _, _, state = self._require_available()
        loaded = load_team_state(state_path, main_root=state.main_root)
        self._validate_capacity(loaded, self._config)
        return loaded

    def _write_state(self, state: TeamPersistentState) -> None:
        state_path, _, _, _ = self._require_available()
        write_team_state(state_path, state)
        self._state = state

    @staticmethod
    def _team(state: TeamPersistentState, team_id: str) -> TeamRecord:
        for team in state.teams:
            if team.team_id == team_id:
                return team
        raise TeamError("team_not_found", "Team 不存在")

    @staticmethod
    def _active_team(state: TeamPersistentState) -> TeamRecord:
        if state.active_team_id is None:
            raise TeamError("team_not_found", "当前没有 active Team")
        return TeamManager._team(state, state.active_team_id)

    @staticmethod
    def _replace_team(
        state: TeamPersistentState,
        replacement: TeamRecord,
        *,
        active_team_id: str | None | object = ...,
    ) -> TeamPersistentState:
        teams = tuple(
            sorted(
                (
                    replacement if item.team_id == replacement.team_id else item
                    for item in state.teams
                ),
                key=lambda item: item.team_id,
            )
        )
        active = (
            state.active_team_id
            if active_team_id is ...
            else active_team_id
        )
        return TeamPersistentState(state.main_root, active, teams)  # type: ignore[arg-type]

    def _member_role_available(self, member: TeamMemberRecord) -> bool:
        definition = self._catalog.get(member.role)
        return definition is not None and definition.isolation == "worktree"

    def _roles_available(self, team: TeamRecord) -> bool:
        return all(self._member_role_available(member) for member in team.members)

    def _mailbox_path(self, team_id: str, recipient: str) -> Path:
        _, _, data_root, _ = self._require_available()
        return data_root / team_id / "mailboxes" / f"{recipient}.jsonl"

    def _history_path(self, team_id: str, member_id: str) -> Path:
        _, _, data_root, _ = self._require_available()
        return data_root / team_id / "histories" / f"{member_id}.jsonl"

    def _validated_mailbox(
        self,
        team: TeamRecord,
        recipient: str,
    ) -> tuple[TeamMailboxMessage, ...]:
        messages = load_mailbox(self._mailbox_path(team.team_id, recipient))
        member_names = {item.name for item in team.members}
        for message in messages:
            if (
                message.team_id != team.team_id
                or message.recipient != recipient
                or (
                    message.sender not in ("lead", "system")
                    and message.sender not in member_names
                )
            ):
                raise TeamError(
                    "team_mailbox_invalid",
                    "Mailbox sender、recipient 或 team 不匹配",
                )
        return messages

    async def _recover_startup(self) -> None:
        _, lock_path, _, state = self._require_available()
        active = None
        if state.active_team_id is not None:
            active = self._team(state, state.active_team_id)
            try:
                integration = next(
                    (
                        item
                        for item in self._worktree_manager.list_records()
                        if item.name == active.integration_worktree_name
                    ),
                    None,
                )
                if (
                    integration is None
                    or integration.kind != "manual"
                    or integration.owner_id is not None
                    or integration.base_head != active.base_head
                ):
                    raise TeamError(
                        "team_repository_unavailable",
                        "Team integration worktree 记录缺失或不匹配",
                    )
                await self._worktree_manager.create(
                    active.integration_worktree_name,
                    kind="manual",
                )
            except TeamError:
                raise
            except WorktreeError as exc:
                raise TeamError(
                    "team_repository_unavailable",
                    "Team integration worktree 恢复失败",
                ) from exc
        current = self._current_time()
        interrupted: list[tuple[TeamRecord, TeamTaskRecord]] = []
        with team_state_lock(lock_path, now=lambda: current):
            state = self._load_state()
            if state.active_team_id is None:
                self._state = state
                return
            team = self._active_team(state)
            missing_roles = {
                member.name
                for member in team.members
                if not self._member_role_available(member)
            }
            tasks: list[TeamTaskRecord] = []
            for task in team.tasks:
                if task.status == "running":
                    recovered = replace(
                        task,
                        status="failed",
                        updated_at=current.isoformat(),
                        ended_at=current.isoformat(),
                        result=None,
                        error_code="team_member_interrupted",
                    )
                    tasks.append(recovered)
                    interrupted.append((team, recovered))
                else:
                    tasks.append(task)
            members = tuple(
                replace(
                    member,
                    state="offline" if member.name in missing_roles else "idle",
                    current_task_id=None,
                    updated_at=(
                        current.isoformat()
                        if member.state == "running" or member.name in missing_roles
                        else member.updated_at
                    ),
                )
                for member in team.members
            )
            target_state = "paused" if missing_roles else team.state
            changed = bool(
                interrupted
                or members != team.members
                or target_state != team.state
            )
            recovered_team = replace(
                team,
                state=target_state,
                updated_at=current.isoformat() if changed else team.updated_at,
                members=members,
                tasks=tuple(tasks),
            )
            recovered_state = self._replace_team(state, recovered_team)
            for _, task in interrupted:
                append_mailbox_message(
                    self._mailbox_path(team.team_id, "lead"),
                    TeamMailboxMessage(
                        self._new_hex_id("message_id"),
                        team.team_id,
                        "system",
                        "lead",
                        "system",
                        current.isoformat(),
                        _terminal_content(
                            task,
                            "failed",
                            None,
                            "team_member_interrupted",
                        ),
                    ),
                )
            if changed:
                self._write_state(recovered_state)
            else:
                self._state = state

    def start(self) -> None:
        self._require_available()
        self._require_accepting()
        if self._scheduler_task is not None:
            return
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(),
            name="mewcode-team-scheduler",
        )
        self._wake.set()

    async def _scheduler_loop(self) -> None:
        try:
            while self._accepting:
                await self._schedule_once()
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=self._config.scheduler_interval_seconds,
                    )
                except TimeoutError:
                    pass
                self._wake.clear()
        except asyncio.CancelledError:
            raise
        except TeamError as exc:
            self._unavailable_error = exc

    @staticmethod
    def _refresh_readiness(
        tasks: Sequence[TeamTaskRecord],
        timestamp: str,
    ) -> tuple[TeamTaskRecord, ...]:
        by_id = {item.task_id: item for item in tasks}
        return tuple(
            replace(task, status="pending", updated_at=timestamp)
            if task.status == "blocked"
            and all(
                by_id[dependency].status in ("completed", "integrated")
                for dependency in task.dependencies
            )
            else task
            for task in tasks
        )

    async def _schedule_once(self) -> None:
        assignments: list[str] = []
        async with self._operation_lock:
            if not self._accepting:
                return
            _, lock_path, _, _ = self._require_available()
            current = self._current_time()
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                if state.active_team_id is None:
                    self._state = state
                    return
                team = self._active_team(state)
                if team.state != "active":
                    self._state = state
                    return
                if not self._roles_available(team):
                    paused = replace(
                        team,
                        state="paused",
                        updated_at=current.isoformat(),
                        members=tuple(
                            replace(
                                member,
                                state=(
                                    member.state
                                    if self._member_role_available(member)
                                    else "offline"
                                ),
                                updated_at=(
                                    member.updated_at
                                    if self._member_role_available(member)
                                    else current.isoformat()
                                ),
                            )
                            for member in team.members
                        ),
                    )
                    self._write_state(self._replace_team(state, paused))
                    return
                tasks = list(
                    self._refresh_readiness(team.tasks, current.isoformat())
                )
                members = {item.name: item for item in team.members}
                idle = {
                    item.name
                    for item in team.members
                    if item.state == "idle"
                }
                ready = sorted(
                    (item for item in tasks if item.status == "pending"),
                    key=lambda item: (item.created_at, item.task_id),
                )
                for task in ready:
                    if task.assignee is not None:
                        selected = task.assignee if task.assignee in idle else None
                    else:
                        selected = min(idle) if idle else None
                    if selected is None:
                        continue
                    idle.remove(selected)
                    index = next(
                        position
                        for position, item in enumerate(tasks)
                        if item.task_id == task.task_id
                    )
                    tasks[index] = replace(
                        task,
                        status="running",
                        assignee=selected,
                        updated_at=current.isoformat(),
                        started_at=current.isoformat(),
                    )
                    members[selected] = replace(
                        members[selected],
                        state="running",
                        current_task_id=task.task_id,
                        updated_at=current.isoformat(),
                    )
                    assignments.append(task.task_id)
                    append_mailbox_message(
                        self._mailbox_path(team.team_id, selected),
                        TeamMailboxMessage(
                            self._new_hex_id("message_id"),
                            team.team_id,
                            "lead",
                            selected,
                            "assignment",
                            current.isoformat(),
                            f"task_id={task.task_id}; title={task.title}",
                        ),
                    )
                updated_team = replace(
                    team,
                    updated_at=(
                        current.isoformat()
                        if tuple(tasks) != team.tasks or assignments
                        else team.updated_at
                    ),
                    members=tuple(sorted(members.values(), key=lambda item: item.name)),
                    tasks=tuple(sorted(tasks, key=lambda item: item.task_id)),
                )
                updated_state = self._replace_team(state, updated_team)
                if updated_state != state:
                    self._write_state(updated_state)
                else:
                    self._state = state
            for task_id in assignments:
                self._episode_ready[task_id] = asyncio.Event()
                episode = asyncio.create_task(
                    self._run_episode(team.team_id, task_id),
                    name=f"mewcode-team-member-{task_id}",
                )
                self._episodes[task_id] = episode
                episode.add_done_callback(
                    lambda completed, identifier=task_id: self._episode_done(
                        identifier, completed
                    )
                )

    def _episode_done(
        self,
        task_id: str,
        task: asyncio.Task[bool],
    ) -> None:
        if self._episodes.get(task_id) is task:
            self._episodes.pop(task_id, None)
            self._episode_ready.pop(task_id, None)

    async def _episode_request(
        self,
        team_id: str,
        task_id: str,
    ) -> tuple[TeamBackendRequest, int, str]:
        async with self._operation_lock:
            _, lock_path, _, _ = self._require_available()
            current = self._current_time()
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                team = self._team(state, team_id)
                task = next(
                    (item for item in team.tasks if item.task_id == task_id),
                    None,
                )
                if task is None or task.status != "running" or task.assignee is None:
                    raise TeamError("team_task_terminal", "Team task 已不再运行")
                member = next(
                    item for item in team.members if item.name == task.assignee
                )
                mailbox = self._validated_mailbox(team, member.name)
                if member.mailbox_cursor > len(mailbox):
                    raise TeamError(
                        "team_mailbox_invalid",
                        "Member mailbox_cursor 超出 mailbox",
                    )
                unread = mailbox[member.mailbox_cursor :]
                history = load_member_history(
                    self._history_path(team.team_id, member.member_id),
                    limit=self._config.member_history_messages,
                )
                by_id = {item.task_id: item for item in team.tasks}
                dependencies = tuple(
                    TeamDependencyResult(
                        dependency,
                        by_id[dependency].title,
                        by_id[dependency].status,  # type: ignore[arg-type]
                        by_id[dependency].result or "Completed without result.",
                    )
                    for dependency in task.dependencies
                )
                return (
                    TeamBackendRequest(
                        team.team_id,
                        member,
                        task,
                        dependencies,
                        unread,
                        history,
                    ),
                    len(mailbox),
                    _history_user_content(task, unread),
                )

    async def _run_episode(self, team_id: str, task_id: str) -> bool:
        try:
            request, mailbox_cursor, history_user = await self._episode_request(
                team_id,
                task_id,
            )
            ready = self._episode_ready.get(task_id)
            cancellation_reason = self._cancel_reasons.get(task_id)
            if cancellation_reason is not None:
                if ready is not None:
                    ready.set()
                return await self._persist_episode(
                    team_id,
                    task_id,
                    TeamBackendResult(
                        "cancelled",
                        None,
                        cancellation_reason,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ),
                    mailbox_cursor,
                    history_user,
                )
            try:
                backend_call = asyncio.create_task(
                    self._backend.start(request),
                    name=f"mewcode-team-backend-{task_id}",
                )
                await asyncio.sleep(0)
                if ready is not None:
                    ready.set()
                async with asyncio.timeout(self._config.member_timeout_seconds):
                    result = await backend_call
            except TimeoutError:
                await self._backend.cancel(task_id)
                result = TeamBackendResult(
                    "failed",
                    None,
                    "team_backend_failed",
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            except TeamError:
                result = TeamBackendResult(
                    "failed",
                    None,
                    "team_backend_failed",
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            return await self._persist_episode(
                team_id,
                task_id,
                result,
                mailbox_cursor,
                history_user,
            )
        except asyncio.CancelledError:
            raise
        except TeamError:
            return False
        finally:
            ready = self._episode_ready.get(task_id)
            if ready is not None:
                ready.set()

    async def _persist_episode(
        self,
        team_id: str,
        task_id: str,
        result: TeamBackendResult,
        mailbox_cursor: int,
        history_user: str,
    ) -> bool:
        async with self._operation_lock:
            _, lock_path, _, _ = self._require_available()
            current = self._current_time()
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                team = self._team(state, team_id)
                task = next(
                    (item for item in team.tasks if item.task_id == task_id),
                    None,
                )
                if task is None or task.status != "running" or task.assignee is None:
                    return False
                member = next(
                    item for item in team.members if item.name == task.assignee
                )
                error_code = result.error_code
                result_state = result.state
                cancellation_reason = self._cancel_reasons.pop(task_id, None)
                if cancellation_reason is not None:
                    result_state = "cancelled"
                    error_code = cancellation_reason
                if result.workspace_path is None:
                    if result_state == "completed":
                        result_state = "failed"
                    if cancellation_reason is None:
                        error_code = "team_backend_failed"
                if result_state == "completed":
                    terminal = replace(
                        task,
                        status="completed",
                        updated_at=current.isoformat(),
                        ended_at=current.isoformat(),
                        result=result.result,
                        error_code=None,
                        workspace_path=result.workspace_path,
                        workspace_preserved=result.workspace_preserved,
                        workspace_reason=result.workspace_reason,
                        branch=result.branch,
                        head=result.head,
                    )
                    assistant_history = result.result or "Completed."
                else:
                    stable_error = error_code or "team_backend_failed"
                    terminal = replace(
                        task,
                        status=result_state,
                        updated_at=current.isoformat(),
                        ended_at=current.isoformat(),
                        result=None,
                        error_code=stable_error,
                        workspace_path=result.workspace_path,
                        workspace_preserved=result.workspace_preserved,
                        workspace_reason=result.workspace_reason,
                        branch=result.branch,
                        head=result.head,
                    )
                    assistant_history = f"Task ended with error_code={stable_error}."
                tasks = self._refresh_readiness(
                    tuple(
                        terminal if item.task_id == task_id else item
                        for item in team.tasks
                    ),
                    current.isoformat(),
                )
                members = tuple(
                    replace(
                        item,
                        state=(
                            "idle" if self._member_role_available(item) else "offline"
                        ),
                        current_task_id=None,
                        mailbox_cursor=mailbox_cursor,
                        updated_at=current.isoformat(),
                    )
                    if item.name == member.name
                    else item
                    for item in team.members
                )
                updated_team = replace(
                    team,
                    updated_at=current.isoformat(),
                    members=members,
                    tasks=tuple(sorted(tasks, key=lambda item: item.task_id)),
                )
                updated_state = self._replace_team(state, updated_team)
                append_member_history(
                    self._history_path(team.team_id, member.member_id),
                    history_user,
                    assistant_history,
                )
                append_mailbox_message(
                    self._mailbox_path(team.team_id, "lead"),
                    TeamMailboxMessage(
                        self._new_hex_id("message_id"),
                        team.team_id,
                        member.name,
                        "lead",
                        "result",
                        current.isoformat(),
                        _terminal_content(
                            terminal,
                            result_state,
                            terminal.result,
                            terminal.error_code,
                        ),
                    ),
                )
                self._write_state(updated_state)
        self._wake.set()
        return True

    async def create_team(
        self,
        name: str,
        members: tuple[tuple[str, str], ...],
    ) -> TeamRecord:
        self._require_accepting()
        try:
            validate_team_name(name)
        except (TypeError, ValueError) as exc:
            raise TeamError("team_name_invalid", "Team name 无效") from exc
        try:
            if not isinstance(members, tuple) or not members:
                raise ValueError("members")
            normalized: list[tuple[str, str]] = []
            for item in members:
                if not isinstance(item, tuple) or len(item) != 2:
                    raise ValueError("member")
                member_name, role = item
                validate_member_name(member_name)
                validate_team_name(role)
                normalized.append((member_name, role))
            if len({item[0] for item in normalized}) != len(normalized):
                raise ValueError("duplicate member")
        except (TypeError, ValueError) as exc:
            raise TeamError("team_member_invalid", "Team members 无效") from exc
        if len(normalized) > self._config.max_members_per_team:
            raise TeamError("team_capacity_reached", "Team member 容量已满")
        for _, role in normalized:
            definition = self._catalog.get(role)
            if definition is None or definition.isolation != "worktree":
                raise TeamError(
                    "team_role_unavailable",
                    "Team member role 不存在或未启用 worktree isolation",
                )
        async with self._operation_lock:
            _, lock_path, _, _ = self._require_available()
            current = self._current_time()
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                if state.active_team_id is not None:
                    raise TeamError("team_active_exists", "已有 active Team")
                if len(state.teams) >= self._config.max_teams:
                    raise TeamError("team_capacity_reached", "Team 容量已满")
                if any(item.name == name for item in state.teams):
                    raise TeamError("team_name_invalid", "Team name 已存在")
            team_id = self._new_team_id()
            integration_name = f"team/{team_id}/integration"
            try:
                created = await self._worktree_manager.create(
                    integration_name,
                    kind="manual",
                )
            except WorktreeError as exc:
                raise TeamError(
                    "team_repository_unavailable",
                    "Team integration worktree 创建失败",
                ) from exc
            member_records = tuple(
                sorted(
                    (
                        TeamMemberRecord(
                            self._new_hex_id("member_id"),
                            member_name,
                            role,
                            "in_process",
                            "idle",
                            None,
                            0,
                            current.isoformat(),
                            current.isoformat(),
                        )
                        for member_name, role in normalized
                    ),
                    key=lambda item: item.name,
                )
            )
            team = TeamRecord(
                team_id,
                name,
                "active",
                created.record.base_head,
                integration_name,
                0,
                current.isoformat(),
                current.isoformat(),
                member_records,
                (),
                (),
            )
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                if state.active_team_id is not None:
                    raise TeamError("team_active_exists", "已有 active Team")
                if len(state.teams) >= self._config.max_teams:
                    raise TeamError("team_capacity_reached", "Team 容量已满")
                if any(item.team_id == team_id or item.name == name for item in state.teams):
                    raise TeamError("team_state_invalid", "Team ID 或 name 冲突")
                updated = TeamPersistentState(
                    state.main_root,
                    team_id,
                    tuple(sorted((*state.teams, team), key=lambda item: item.team_id)),
                )
                self._write_state(updated)
        self._wake.set()
        return team

    async def list_teams(self) -> tuple[TeamRecord, ...]:
        async with self._operation_lock:
            state = self._load_state()
            self._state = state
            return state.teams

    async def get_team(self, team_id: str | None = None) -> TeamRecord:
        async with self._operation_lock:
            state = self._load_state()
            self._state = state
            if team_id is None:
                return self._active_team(state)
            return self._team(state, team_id)

    async def create_task(
        self,
        title: str,
        instructions: str,
        *,
        assignee: str | None = None,
        depends_on: tuple[str, ...] = (),
    ) -> TeamTaskRecord:
        self._require_accepting()
        async with self._operation_lock:
            _, lock_path, _, _ = self._require_available()
            current = self._current_time()
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                team = self._active_team(state)
                if team.state != "active":
                    raise TeamError("team_paused", "Team 已暂停")
                if len(team.tasks) >= self._config.max_tasks_per_team:
                    raise TeamError("team_capacity_reached", "Team task 容量已满")
                if assignee is not None and assignee not in {
                    item.name for item in team.members
                }:
                    raise TeamError("team_member_not_found", "Team member 不存在")
                if (
                    not isinstance(depends_on, tuple)
                    or len(depends_on) != len(set(depends_on))
                    or not set(depends_on).issubset(
                        {item.task_id for item in team.tasks}
                    )
                ):
                    raise TeamError("team_dependency_invalid", "Task dependency 无效")
                by_id = {item.task_id: item for item in team.tasks}
                status = (
                    "pending"
                    if all(
                        by_id[item].status in ("completed", "integrated")
                        for item in depends_on
                    )
                    else "blocked"
                )
                try:
                    task = TeamTaskRecord(
                        self._new_hex_id("task_id"),
                        title,
                        instructions,
                        status,
                        assignee,
                        depends_on,
                        current.isoformat(),
                        current.isoformat(),
                    )
                except ValueError as exc:
                    raise TeamError("team_task_invalid", "Team task 参数无效") from exc
                if any(item.task_id == task.task_id for item in team.tasks):
                    raise TeamError("team_state_invalid", "Team task ID 冲突")
                updated_team = replace(
                    team,
                    updated_at=current.isoformat(),
                    tasks=tuple(
                        sorted((*team.tasks, task), key=lambda item: item.task_id)
                    ),
                )
                self._write_state(self._replace_team(state, updated_team))
        self._wake.set()
        return task

    async def list_tasks(self) -> tuple[TeamTaskRecord, ...]:
        return (await self.get_team()).tasks

    async def get_task(self, task_id: str) -> TeamTaskRecord:
        try:
            validate_team_hex_id(task_id, "task_id")
        except ValueError as exc:
            raise TeamError("team_task_invalid", "Task ID 无效") from exc
        team = await self.get_team()
        task = next((item for item in team.tasks if item.task_id == task_id), None)
        if task is None:
            raise TeamError("team_task_not_found", "Team task 不存在")
        return task

    async def cancel_task(self, task_id: str) -> TeamTaskRecord:
        self._require_accepting()
        episode: asyncio.Task[bool] | None = None
        running = False
        async with self._operation_lock:
            _, lock_path, _, _ = self._require_available()
            current = self._current_time()
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                team = self._active_team(state)
                task = next(
                    (item for item in team.tasks if item.task_id == task_id),
                    None,
                )
                if task is None:
                    raise TeamError("team_task_not_found", "Team task 不存在")
                if task.status in _TERMINAL_TASK_STATES:
                    raise TeamError("team_task_terminal", "Team task 已终止")
                if task.status == "running":
                    running = True
                    self._cancel_reasons[task_id] = "worker_cancelled"
                    episode = self._episodes.get(task_id)
                else:
                    cancelled = replace(
                        task,
                        status="cancelled",
                        updated_at=current.isoformat(),
                        ended_at=current.isoformat(),
                        error_code="worker_cancelled",
                    )
                    updated_team = replace(
                        team,
                        updated_at=current.isoformat(),
                        tasks=tuple(
                            cancelled if item.task_id == task_id else item
                            for item in team.tasks
                        ),
                    )
                    append_mailbox_message(
                        self._mailbox_path(team.team_id, "lead"),
                        TeamMailboxMessage(
                            self._new_hex_id("message_id"),
                            team.team_id,
                            "system",
                            "lead",
                            "system",
                            current.isoformat(),
                            _terminal_content(
                                cancelled,
                                "cancelled",
                                None,
                                "worker_cancelled",
                            ),
                        ),
                    )
                    self._write_state(self._replace_team(state, updated_team))
        if running:
            ready = self._episode_ready.get(task_id)
            if ready is not None:
                await ready.wait()
            await self._backend.cancel(task_id)
            if episode is not None:
                await asyncio.gather(episode, return_exceptions=True)
        self._wake.set()
        return await self.get_task(task_id)

    async def send_message(
        self,
        recipient: str,
        content: str,
    ) -> TeamMailboxMessage:
        self._require_accepting()
        async with self._operation_lock:
            _, lock_path, _, _ = self._require_available()
            current = self._current_time()
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                team = self._active_team(state)
                if recipient not in {item.name for item in team.members}:
                    raise TeamError("team_member_not_found", "Team member 不存在")
                try:
                    message = TeamMailboxMessage(
                        self._new_hex_id("message_id"),
                        team.team_id,
                        "lead",
                        recipient,
                        "message",
                        current.isoformat(),
                        content,
                    )
                except ValueError as exc:
                    raise TeamError("team_mailbox_invalid", "Team message 无效") from exc
                append_mailbox_message(
                    self._mailbox_path(team.team_id, recipient),
                    message,
                )
                self._state = state
        self._wake.set()
        return message

    async def pause(self) -> TeamRecord:
        self._require_accepting()
        async with self._operation_lock:
            _, lock_path, _, _ = self._require_available()
            current = self._current_time()
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                team = self._active_team(state)
                if team.state == "paused":
                    self._state = state
                    return team
                paused = replace(
                    team,
                    state="paused",
                    updated_at=current.isoformat(),
                )
                self._write_state(self._replace_team(state, paused))
                return paused

    async def resume(self) -> TeamRecord:
        self._require_accepting()
        async with self._operation_lock:
            _, lock_path, _, _ = self._require_available()
            current = self._current_time()
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                team = self._active_team(state)
                if not self._roles_available(team):
                    raise TeamError("team_role_unavailable", "Team member role 不可用")
                members = tuple(
                    replace(
                        member,
                        state="idle",
                        updated_at=current.isoformat(),
                    )
                    if member.state == "offline"
                    else member
                    for member in team.members
                )
                resumed = replace(
                    team,
                    state="active",
                    updated_at=current.isoformat(),
                    members=members,
                )
                self._write_state(self._replace_team(state, resumed))
        self._wake.set()
        return resumed

    async def close_team(self) -> TeamRecord:
        self._require_accepting()
        await self.pause()
        team = await self.get_team()
        running = tuple(
            task.task_id for task in team.tasks if task.status == "running"
        )
        for task_id in running:
            self._cancel_reasons[task_id] = "team_shutdown"
            ready = self._episode_ready.get(task_id)
            if ready is not None:
                await ready.wait()
            await self._backend.cancel(task_id)
        episodes = tuple(
            self._episodes[task_id]
            for task_id in running
            if task_id in self._episodes
        )
        if episodes:
            await asyncio.gather(*episodes, return_exceptions=True)
        async with self._operation_lock:
            _, lock_path, _, _ = self._require_available()
            current = self._current_time()
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                team = self._active_team(state)
                closed = replace(
                    team,
                    state="closed",
                    updated_at=current.isoformat(),
                )
                self._write_state(
                    self._replace_team(state, closed, active_team_id=None)
                )
        self._wake.set()
        return closed

    async def take_lead_notifications(self) -> tuple[dict[str, object], ...]:
        async with self._operation_lock:
            _, lock_path, _, _ = self._require_available()
            current = self._current_time()
            with team_state_lock(lock_path, now=lambda: current):
                state = self._load_state()
                remaining = 32
                controls: list[dict[str, object]] = []
                replacements: dict[str, TeamRecord] = {}
                for team in state.teams:
                    if remaining == 0:
                        break
                    messages = self._validated_mailbox(team, "lead")
                    if team.lead_mailbox_cursor > len(messages):
                        raise TeamError(
                            "team_mailbox_invalid",
                            "Lead mailbox_cursor 超出 mailbox",
                        )
                    batch = messages[
                        team.lead_mailbox_cursor : team.lead_mailbox_cursor + remaining
                    ]
                    if not batch:
                        continue
                    controls.extend(
                        {
                            "type": "team_notification",
                            "team_id": item.team_id,
                            "message_id": item.message_id,
                            "sender": item.sender,
                            "kind": item.kind,
                            "content": item.content,
                        }
                        for item in batch
                    )
                    replacements[team.team_id] = replace(
                        team,
                        lead_mailbox_cursor=team.lead_mailbox_cursor + len(batch),
                    )
                    remaining -= len(batch)
                if replacements:
                    updated = TeamPersistentState(
                        state.main_root,
                        state.active_team_id,
                        tuple(
                            replacements.get(team.team_id, team)
                            for team in state.teams
                        ),
                    )
                    self._write_state(updated)
                else:
                    self._state = state
                return tuple(controls)

    async def close(self) -> TeamCloseResult:
        async with self._operation_lock:
            if self._close_result is not None:
                return self._close_result
            self._accepting = False
            scheduler = self._scheduler_task
            self._scheduler_task = None
            active_items = tuple(self._episodes.items())
            for task_id, _ in active_items:
                self._cancel_reasons[task_id] = "team_shutdown"
        if scheduler is not None:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
        cancelled = 0
        for task_id, task in active_items:
            ready = self._episode_ready.get(task_id)
            if ready is not None:
                await ready.wait()
            if not task.done() and await self._backend.cancel(task_id):
                cancelled += 1
        results = (
            await asyncio.gather(
                *(task for _, task in active_items),
                return_exceptions=True,
            )
            if active_items
            else ()
        )
        persisted = sum(result is True for result in results)
        await self._backend.close()
        async with self._operation_lock:
            self._close_result = TeamCloseResult(
                len(active_items),
                cancelled,
                persisted,
            )
            return self._close_result
