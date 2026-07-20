from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mewcode_agent.teams import (
    TeamBackendRequest,
    TeamBackendResult,
    TeamError,
    TeamManager,
    TeamMemberRecord,
    TeamPersistentState,
    TeamRecord,
    TeamRuntimeConfig,
    TeamTaskRecord,
    load_member_history,
    write_team_state,
)
from mewcode_agent.workers import (
    WorkerCatalog,
    WorkerCatalogSnapshot,
    WorkerRoleDefinition,
    WorkerRuntimeConfig,
)
from mewcode_agent.worktrees import (
    GitCommandResult,
    GitRepositoryIdentity,
    WorktreeCreateResult,
    WorktreeRecord,
    WorktreeStatus,
    managed_worktree_path,
    worktree_branch_name,
)


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
TASK_HEAD = "b" * 40
INTEGRATED_HEAD = "c" * 40
FINAL_HEAD = "d" * 40


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.value:032x}"


class FakeGit:
    def __init__(self, root: Path, common: Path) -> None:
        self.root = root
        self.common = common

    async def repository_identity(self, _cwd: Path) -> GitRepositoryIdentity:
        return GitRepositoryIdentity(self.root, self.common)


class FakeWorktrees:
    def __init__(self, root: Path) -> None:
        self.available = True
        self.main_root = root
        self.managed_root = (root / ".mewcode" / ".worktrees").resolve()
        self.records: dict[str, WorktreeRecord] = {}
        self.heads: dict[str, str] = {}
        self.dirty: set[str] = set()
        self.reason_codes: dict[str, str] = {}
        self.protected: set[str] = set()

    def list_records(self) -> tuple[WorktreeRecord, ...]:
        return tuple(self.records[name] for name in sorted(self.records))

    async def create(
        self,
        name: str,
        *,
        kind: str = "manual",
        owner_id: str | None = None,
    ) -> WorktreeCreateResult:
        existing = self.records.get(name)
        if existing is not None:
            return WorktreeCreateResult(existing, True)
        record = WorktreeRecord(
            name,
            managed_worktree_path(self.managed_root, name),
            worktree_branch_name(name),
            HEAD,
            kind,  # type: ignore[arg-type]
            owner_id,
            NOW.isoformat(),
            NOW.isoformat(),
            (NOW + timedelta(hours=72)).isoformat(),
        )
        self.records[name] = record
        self.heads[name] = HEAD
        return WorktreeCreateResult(record, False)

    async def status(self, name: str) -> WorktreeStatus:
        record = self.records.get(name)
        if record is None:
            raise AssertionError(f"unknown worktree: {name}")
        dirty = name in self.dirty
        head = self.heads[name]
        return WorktreeStatus(
            True,
            head,
            dirty,
            1 if dirty else 0,
            None,
            1 if head != record.base_head else 0,
            head != record.base_head,
            not dirty and head == record.base_head,
            self.reason_codes.get(name),
        )

    async def protect(self, name: str) -> None:
        if name not in self.records:
            raise AssertionError(f"unknown worktree: {name}")
        self.protected.add(name)


class MergeGit(FakeGit):
    def __init__(
        self,
        root: Path,
        common: Path,
        worktrees: FakeWorktrees,
    ) -> None:
        super().__init__(root, common)
        self.worktrees = worktrees
        self.main_head = HEAD
        self.main_dirty = False
        self.task_conflict = False
        self.absorb_conflict = False
        self.ff_failure = False
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def _worktree_name(self, cwd: Path) -> str | None:
        return next(
            (name for name, record in self.worktrees.records.items() if record.path == cwd),
            None,
        )

    async def run(
        self,
        cwd: Path,
        *arguments: str,
        timeout_seconds: float = 30.0,
        check: bool = True,
        error_code: str = "worktree_status_failed",
    ) -> GitCommandResult:
        del timeout_seconds, check, error_code
        self.calls.append((cwd, arguments))
        name = self._worktree_name(cwd)
        if arguments == ("rev-parse", "HEAD"):
            return GitCommandResult(
                0,
                self.main_head if cwd == self.root else self.worktrees.heads[name],
            )
        if arguments == (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
        ):
            return GitCommandResult(0, "dirty\x00" if self.main_dirty else "")
        if arguments[:3] == ("merge", "--no-ff", "--no-edit"):
            if self.task_conflict:
                return GitCommandResult(1, "")
            assert name is not None
            self.worktrees.heads[name] = INTEGRATED_HEAD
            return GitCommandResult(0, "")
        if arguments[:2] == ("merge", "--no-edit"):
            if self.absorb_conflict:
                return GitCommandResult(1, "")
            assert name is not None
            self.worktrees.heads[name] = FINAL_HEAD
            return GitCommandResult(0, "")
        if arguments[:2] == ("merge", "--ff-only"):
            if self.ff_failure:
                return GitCommandResult(1, "")
            assert cwd == self.root
            integration_name = next(
                item for item in self.worktrees.records if item.startswith("team/")
            )
            self.main_head = self.worktrees.heads[integration_name]
            return GitCommandResult(0, "")
        if arguments == ("merge", "--abort"):
            return GitCommandResult(0, "")
        raise AssertionError(f"unexpected Git arguments: {arguments}")


class RecordingBackend:
    def __init__(self, root: Path, *, blocked: bool = False) -> None:
        self.root = root
        self.blocked = blocked
        self.requests: list[TeamBackendRequest] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.cancelled: set[str] = set()
        self.closed = 0
        self.failures: set[str] = set()
        self.heads: dict[str, str] = {}
        self.workspaces: dict[str, Path] = {}
        self.branches: dict[str, str] = {}
        self.worktree_manager: FakeWorktrees | None = None

    async def start(self, request: TeamBackendRequest) -> TeamBackendResult:
        self.requests.append(request)
        gate = self.gates.setdefault(request.task.task_id, asyncio.Event())
        if self.blocked:
            await gate.wait()
        task_id = request.task.task_id
        head = self.heads.get(task_id, HEAD)
        if self.worktree_manager is not None and task_id not in self.workspaces:
            created = await self.worktree_manager.create(
                f"worker/{task_id}",
                kind="worker",
                owner_id=task_id,
            )
            self.workspaces[task_id] = created.record.path
            self.branches[task_id] = created.record.branch
            self.worktree_manager.heads[created.record.name] = head
        workspace = self.workspaces.get(
            task_id,
            (self.root / "workers" / task_id).resolve(),
        )
        branch = self.branches.get(task_id, f"branch-{task_id}")
        if task_id in self.cancelled:
            return TeamBackendResult(
                "cancelled",
                None,
                "worker_cancelled",
                workspace,
                True,
                "team_integration_pending",
                branch,
                head,
            )
        if task_id in self.failures:
            return TeamBackendResult(
                "failed",
                None,
                "worker_failed",
                workspace,
                True,
                "worktree_dirty",
                branch,
                head,
            )
        return TeamBackendResult(
            "completed",
            f"completed {request.task.title}",
            None,
            workspace,
            True,
            "team_integration_pending",
            branch,
            head,
        )

    async def cancel(self, task_id: str) -> bool:
        self.cancelled.add(task_id)
        gate = self.gates.get(task_id)
        if gate is None:
            return False
        gate.set()
        return True

    async def close(self) -> None:
        self.closed += 1
        for gate in self.gates.values():
            gate.set()

    def release(self, task_id: str) -> None:
        self.gates[task_id].set()


def _catalog(tmp_path: Path, *names: str) -> WorkerCatalog:
    definitions = tuple(
        WorkerRoleDefinition(
            name,
            f"{name} role",
            None,
            (),
            "inherit",
            5,
            "inherit",
            "worktree",
            "Use exact evidence.",
            "project",
            tmp_path.resolve(),
            (tmp_path / f"{name}.md").resolve(),
        )
        for name in sorted(names)
    )
    return WorkerCatalog(
        WorkerCatalogSnapshot(definitions, (), WorkerRuntimeConfig())
    )


async def _open_manager(
    tmp_path: Path,
    backend: RecordingBackend,
    *,
    catalog: WorkerCatalog | None = None,
) -> tuple[TeamManager, FakeWorktrees, Path]:
    root = (tmp_path / "repo").resolve()
    common = (tmp_path / "git").resolve()
    root.mkdir(exist_ok=True)
    common.mkdir(exist_ok=True)
    worktrees = FakeWorktrees(root)
    backend.worktree_manager = worktrees
    manager = await TeamManager.open(
        root,
        TeamRuntimeConfig(),
        catalog=catalog or _catalog(tmp_path, "implementer", "reviewer"),
        backend=backend,
        worktree_manager=worktrees,  # type: ignore[arg-type]
        git=FakeGit(root, common),  # type: ignore[arg-type]
        now=lambda: NOW,
        id_factory=SequentialIds(),
    )
    assert manager.available
    return manager, worktrees, common


async def _wait_until(predicate, attempts: int = 200) -> None:
    for _ in range(attempts):
        if await predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


async def test_create_team_validates_role_and_preserves_integration(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend(tmp_path)
    manager, worktrees, _ = await _open_manager(tmp_path, backend)

    team = await manager.create_team(
        "alpha",
        (("review", "reviewer"), ("build", "implementer")),
    )

    assert team.team_id == "t" + "0" * 30 + "1"
    assert [item.name for item in team.members] == ["build", "review"]
    assert team.integration_worktree_name in worktrees.records
    with pytest.raises(TeamError) as caught:
        await manager.create_team("beta", (("build", "implementer"),))
    assert caught.value.code == "team_active_exists"
    await manager.close()


async def test_create_team_rejects_missing_or_non_worktree_role(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend(tmp_path)
    manager, _, _ = await _open_manager(tmp_path, backend)

    with pytest.raises(TeamError) as caught:
        await manager.create_team("alpha", (("build", "missing"),))

    assert caught.value.code == "team_role_unavailable"
    await manager.close()


async def test_scheduler_assigns_ready_tasks_in_deterministic_order(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend(tmp_path)
    manager, _, common = await _open_manager(tmp_path, backend)
    await manager.create_team(
        "alpha",
        (("zeta", "implementer"), ("alpha", "reviewer")),
    )
    first = await manager.create_task("First", "Do first.")
    second = await manager.create_task("Second", "Do second.")

    manager.start()

    async def completed() -> bool:
        tasks = await manager.list_tasks()
        return len(tasks) == 2 and all(item.status == "completed" for item in tasks)

    await _wait_until(completed)
    assignments = {
        request.task.task_id: request.member.name for request in backend.requests
    }
    assert assignments == {first.task_id: "alpha", second.task_id: "zeta"}
    team = await manager.get_team()
    assert all(item.state == "idle" for item in team.members)
    for member in team.members:
        history_path = (
            common
            / "mewcode-agent"
            / "teams"
            / team.team_id
            / "histories"
            / f"{member.member_id}.jsonl"
        )
        assert len(load_member_history(history_path, limit=40)) == 2
    await manager.close()


async def test_dependency_stays_blocked_then_receives_parent_result(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend(tmp_path, blocked=True)
    manager, _, _ = await _open_manager(tmp_path, backend)
    await manager.create_team("alpha", (("build", "implementer"),))
    parent = await manager.create_task("Parent", "Build parent.")
    child = await manager.create_task(
        "Child",
        "Build child.",
        depends_on=(parent.task_id,),
    )
    assert child.status == "blocked"
    manager.start()

    async def parent_started() -> bool:
        return any(item.task.task_id == parent.task_id for item in backend.requests)

    await _wait_until(parent_started)
    assert (await manager.get_task(child.task_id)).status == "blocked"
    backend.release(parent.task_id)

    async def child_started() -> bool:
        return any(item.task.task_id == child.task_id for item in backend.requests)

    await _wait_until(child_started)
    child_request = next(
        item for item in backend.requests if item.task.task_id == child.task_id
    )
    assert child_request.dependencies[0].task_id == parent.task_id
    assert child_request.dependencies[0].result == "completed Parent"
    backend.release(child.task_id)
    await _wait_until(
        lambda: _task_has_status(manager, child.task_id, "completed")
    )
    await manager.close()


async def test_failed_dependency_remains_blocked(tmp_path: Path) -> None:
    backend = RecordingBackend(tmp_path)
    manager, _, _ = await _open_manager(tmp_path, backend)
    await manager.create_team("alpha", (("build", "implementer"),))
    parent = await manager.create_task("Parent", "Fail parent.")
    child = await manager.create_task(
        "Child",
        "Do not start.",
        depends_on=(parent.task_id,),
    )
    backend.failures.add(parent.task_id)
    manager.start()

    await _wait_until(
        lambda: _task_has_status(manager, parent.task_id, "failed")
    )

    assert (await manager.get_task(parent.task_id)).error_code == "worker_failed"
    assert (await manager.get_task(child.task_id)).status == "blocked"
    assert [item.task.task_id for item in backend.requests] == [parent.task_id]
    await manager.close()


async def _task_has_status(
    manager: TeamManager,
    task_id: str,
    status: str,
) -> bool:
    return (await manager.get_task(task_id)).status == status


async def test_mailbox_and_paired_history_flow_into_next_episode(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend(tmp_path)
    manager, _, _ = await _open_manager(tmp_path, backend)
    await manager.create_team("alpha", (("build", "implementer"),))
    first = await manager.create_task("First", "Run first.")
    manager.start()
    await _wait_until(
        lambda: _task_has_status(manager, first.task_id, "completed")
    )
    sent = await manager.send_message("build", "Check cache invalidation.")
    second = await manager.create_task("Second", "Run second.")
    await _wait_until(
        lambda: _task_has_status(manager, second.task_id, "completed")
    )

    request = next(
        item for item in backend.requests if item.task.task_id == second.task_id
    )
    assert sent.message_id in {item.message_id for item in request.mailbox}
    assert len(request.history) == 2
    team = await manager.get_team()
    assert team.members[0].mailbox_cursor >= 3
    await manager.close()


async def test_pause_resume_and_cancel_pending_task(tmp_path: Path) -> None:
    backend = RecordingBackend(tmp_path)
    manager, _, _ = await _open_manager(tmp_path, backend)
    await manager.create_team("alpha", (("build", "implementer"),))
    task = await manager.create_task("Pending", "Wait.")
    await manager.pause()
    manager.start()
    for _ in range(10):
        await asyncio.sleep(0)
    assert backend.requests == []

    cancelled = await manager.cancel_task(task.task_id)

    assert cancelled.status == "cancelled"
    await manager.resume()
    assert (await manager.get_team()).state == "active"
    await manager.close()


async def test_cancel_running_task_persists_user_cancellation(tmp_path: Path) -> None:
    backend = RecordingBackend(tmp_path, blocked=True)
    manager, _, _ = await _open_manager(tmp_path, backend)
    await manager.create_team("alpha", (("build", "implementer"),))
    task = await manager.create_task("Long", "Keep running.")
    manager.start()
    await _wait_until(lambda: _task_has_status(manager, task.task_id, "running"))

    cancelled = await manager.cancel_task(task.task_id)

    assert cancelled.status == "cancelled"
    assert cancelled.error_code == "worker_cancelled"
    assert (await manager.get_team()).members[0].state == "idle"
    await manager.close()


async def test_close_cancels_and_persists_running_episode(tmp_path: Path) -> None:
    backend = RecordingBackend(tmp_path, blocked=True)
    manager, _, _ = await _open_manager(tmp_path, backend)
    await manager.create_team("alpha", (("build", "implementer"),))
    task = await manager.create_task("Long", "Keep running.")
    manager.start()

    async def started() -> bool:
        return bool(backend.requests)

    await _wait_until(started)
    result = await manager.close()

    assert result.active_episodes == 1
    assert result.cancelled_episodes == 1
    assert result.persisted_episodes == 1
    assert (await manager.get_task(task.task_id)).error_code == "team_shutdown"
    assert backend.closed == 1
    assert await manager.close() == result


async def test_close_team_keeps_worktrees_and_clears_active_id(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend(tmp_path)
    manager, worktrees, _ = await _open_manager(tmp_path, backend)
    team = await manager.create_team("alpha", (("build", "implementer"),))
    names_before = tuple(worktrees.records)

    closed = await manager.close_team()

    assert closed.state == "closed"
    assert manager.active_team_id is None
    assert tuple(worktrees.records) == names_before
    assert (await manager.list_teams())[0].team_id == team.team_id
    await manager.close()


async def test_startup_refuses_to_recreate_missing_integration_record(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend(tmp_path)
    manager, _, common = await _open_manager(tmp_path, backend)
    team = await manager.create_team("alpha", (("build", "implementer"),))
    root = (tmp_path / "repo").resolve()
    await manager.close()
    replacement_backend = RecordingBackend(tmp_path)
    missing_worktrees = FakeWorktrees(root)

    recovered = await TeamManager.open(
        root,
        TeamRuntimeConfig(),
        catalog=_catalog(tmp_path, "implementer"),
        backend=replacement_backend,
        worktree_manager=missing_worktrees,  # type: ignore[arg-type]
        git=FakeGit(root, common),  # type: ignore[arg-type]
        now=lambda: NOW,
        id_factory=SequentialIds(),
    )

    assert not recovered.available
    assert team.integration_worktree_name not in missing_worktrees.records
    with pytest.raises(TeamError) as caught:
        await recovered.get_team()
    assert caught.value.code == "team_repository_unavailable"
    await recovered.close()


async def test_lead_notifications_are_persistent_and_consumed_once(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend(tmp_path)
    manager, _, _ = await _open_manager(tmp_path, backend)
    await manager.create_team("alpha", (("build", "implementer"),))
    task = await manager.create_task("Notify", "Complete.")
    manager.start()
    await _wait_until(
        lambda: _task_has_status(manager, task.task_id, "completed")
    )

    first = await manager.take_lead_notifications()
    second = await manager.take_lead_notifications()

    assert len(first) == 1
    assert first[0]["type"] == "team_notification"
    assert first[0]["sender"] == "build"
    assert second == ()
    await manager.close()


async def test_startup_recovers_running_task_and_pauses_missing_role(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "repo").resolve()
    common = (tmp_path / "git").resolve()
    root.mkdir()
    common.mkdir()
    team_id = "t" + "1" * 31
    task_id = "3" * 32
    member = TeamMemberRecord(
        "2" * 32,
        "build",
        "missing",
        "in_process",
        "running",
        task_id,
        0,
        NOW.isoformat(),
        NOW.isoformat(),
    )
    task = TeamTaskRecord(
        task_id,
        "Interrupted",
        "Resume safely.",
        "running",
        "build",
        (),
        NOW.isoformat(),
        NOW.isoformat(),
        started_at=NOW.isoformat(),
    )
    team = TeamRecord(
        team_id,
        "alpha",
        "active",
        HEAD,
        f"team/{team_id}/integration",
        0,
        NOW.isoformat(),
        NOW.isoformat(),
        (member,),
        (task,),
        (),
    )
    state_path = common / "mewcode-agent" / "teams.json"
    write_team_state(state_path, TeamPersistentState(root, team_id, (team,)))
    backend = RecordingBackend(tmp_path)
    worktrees = FakeWorktrees(root)
    await worktrees.create(f"team/{team_id}/integration", kind="manual")

    manager = await TeamManager.open(
        root,
        TeamRuntimeConfig(),
        catalog=_catalog(tmp_path),
        backend=backend,
        worktree_manager=worktrees,  # type: ignore[arg-type]
        git=FakeGit(root, common),  # type: ignore[arg-type]
        now=lambda: NOW + timedelta(minutes=1),
        id_factory=SequentialIds(),
    )

    recovered = await manager.get_team()
    assert recovered.state == "paused"
    assert recovered.members[0].state == "offline"
    assert recovered.tasks[0].status == "failed"
    assert recovered.tasks[0].error_code == "team_member_interrupted"
    notifications = await manager.take_lead_notifications()
    assert notifications[0]["sender"] == "system"
    with pytest.raises(TeamError) as caught:
        await manager.resume()
    assert caught.value.code == "team_role_unavailable"
    await manager.close()


async def _completed_merge_team(
    tmp_path: Path,
) -> tuple[
    TeamManager,
    FakeWorktrees,
    MergeGit,
    RecordingBackend,
    TeamTaskRecord,
]:
    root = (tmp_path / "repo").resolve()
    common = (tmp_path / "git").resolve()
    root.mkdir()
    common.mkdir()
    worktrees = FakeWorktrees(root)
    git = MergeGit(root, common, worktrees)
    backend = RecordingBackend(tmp_path)
    manager = await TeamManager.open(
        root,
        TeamRuntimeConfig(),
        catalog=_catalog(tmp_path, "implementer"),
        backend=backend,
        worktree_manager=worktrees,  # type: ignore[arg-type]
        git=git,  # type: ignore[arg-type]
        now=lambda: NOW,
        id_factory=SequentialIds(),
    )
    await manager.create_team("alpha", (("build", "implementer"),))
    task = await manager.create_task("Feature", "Commit the feature.")
    worker = await worktrees.create(
        f"worker/{task.task_id}",
        kind="worker",
        owner_id=task.task_id,
    )
    worktrees.heads[worker.record.name] = TASK_HEAD
    backend.heads[task.task_id] = TASK_HEAD
    backend.workspaces[task.task_id] = worker.record.path
    backend.branches[task.task_id] = worker.record.branch
    manager.start()
    await _wait_until(
        lambda: _task_has_status(manager, task.task_id, "completed")
    )
    return manager, worktrees, git, backend, await manager.get_task(task.task_id)


async def test_integrate_task_merges_once_and_keeps_worker_worktree(
    tmp_path: Path,
) -> None:
    manager, worktrees, git, _, task = await _completed_merge_team(tmp_path)
    names_before = tuple(worktrees.records)

    integrated = await manager.integrate_task(task.task_id)
    repeated = await manager.integrate_task(task.task_id)

    assert integrated.status == "integrated"
    assert integrated.integrated_head == INTEGRATED_HEAD
    assert repeated == integrated
    assert tuple(worktrees.records) == names_before
    task_merges = [
        arguments
        for _, arguments in git.calls
        if arguments[:3] == ("merge", "--no-ff", "--no-edit")
    ]
    assert task_merges == [
        ("merge", "--no-ff", "--no-edit", task.branch)
    ]
    assert (await manager.get_team()).merged_task_ids == (task.task_id,)
    await manager.close()


async def test_integrate_conflict_aborts_and_preserves_completed_state(
    tmp_path: Path,
) -> None:
    manager, worktrees, git, _, task = await _completed_merge_team(tmp_path)
    git.task_conflict = True
    integration_name = (await manager.get_team()).integration_worktree_name

    with pytest.raises(TeamError) as caught:
        await manager.integrate_task(task.task_id)

    assert caught.value.code == "team_merge_conflict"
    assert (await manager.get_task(task.task_id)).status == "completed"
    assert worktrees.heads[integration_name] == HEAD
    assert any(arguments == ("merge", "--abort") for _, arguments in git.calls)
    await manager.close()


async def test_main_merge_uses_preview_then_fast_forwards_without_cleanup(
    tmp_path: Path,
) -> None:
    manager, worktrees, git, _, task = await _completed_merge_team(tmp_path)
    await manager.integrate_task(task.task_id)
    names_before = tuple(worktrees.records)

    preview = await manager.preview_main_merge()
    merged = await manager.merge_into_main(preview)

    assert preview.main_head == HEAD
    assert preview.integration_head == INTEGRATED_HEAD
    assert preview.main_dirty is False
    assert preview.integration_dirty is False
    assert merged.state == "merged"
    assert manager.active_team_id is None
    assert git.main_head == FINAL_HEAD
    assert tuple(worktrees.records) == names_before
    arguments = [item for _, item in git.calls]
    assert ("merge", "--no-edit", HEAD) in arguments
    assert (
        "merge",
        "--ff-only",
        worktrees.records[merged.integration_worktree_name].branch,
    ) in arguments
    assert all(
        item[0] not in {"push", "reset", "clean", "worktree"}
        for item in arguments
    )
    await manager.close()


async def test_main_merge_rejects_changed_main_before_git_side_effect(
    tmp_path: Path,
) -> None:
    manager, _, git, _, task = await _completed_merge_team(tmp_path)
    await manager.integrate_task(task.task_id)
    preview = await manager.preview_main_merge()
    calls_before = len(git.calls)
    git.main_head = "e" * 40

    with pytest.raises(TeamError) as caught:
        await manager.merge_into_main(preview)

    assert caught.value.code == "team_main_changed"
    new_calls = git.calls[calls_before:]
    assert all(arguments[0] != "merge" for _, arguments in new_calls)
    assert (await manager.get_team()).state == "active"
    await manager.close()
