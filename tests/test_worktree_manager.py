from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess

import pytest

from mewcode_agent.worktrees import (
    GitRunner,
    WorktreeError,
    WorktreeManager,
    WorktreeRuntimeConfig,
    worktree_branch_name,
)
from mewcode_agent.worktrees import manager as manager_module


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _git(root: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git executable is unavailable")
    result = subprocess.run(
        [executable, "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return result.stdout.rstrip("\r\n")


def _repository(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "MewCode Tests")
    _git(root, "config", "user.email", "tests@example.invalid")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "base")
    return root


async def _manager(
    root: Path,
    *,
    config: WorktreeRuntimeConfig | None = None,
    clock=None,
) -> WorktreeManager:
    return await WorktreeManager.open(
        root,
        config or WorktreeRuntimeConfig(local_config_files=()),
        now=clock or (lambda: NOW),
    )


async def test_create_status_and_safe_delete(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    manager = await _manager(root)

    created = await manager.create("feature/cache")
    status = await manager.status("feature/cache")

    assert created.recovered is False
    assert created.record.path.is_dir()
    assert status.exists is True
    assert status.dirty is False
    assert status.has_unpushed is False
    assert status.deletion_safe is True
    exclude = root / ".git" / "info" / "exclude"
    assert exclude.read_text(encoding="utf-8").splitlines().count(
        "/.mewcode/.worktrees/"
    ) == 1
    await manager.delete("feature/cache")
    assert manager.list_records() == ()
    assert not created.record.path.exists()
    await manager.close()


async def test_fast_recover_does_not_run_git(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _repository(tmp_path)
    runner = GitRunner()
    manager = await WorktreeManager.open(
        root,
        WorktreeRuntimeConfig(local_config_files=()),
        git=runner,
        now=lambda: NOW,
    )
    first = await manager.create("feature/recover")

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("fast recover launched Git")

    monkeypatch.setattr(runner, "run", fail_if_called)
    recovered = await manager.create("feature/recover")

    assert first.record.path == recovered.record.path
    assert recovered.recovered is True
    await manager.close()


async def test_path_conflict_is_fail_closed(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    manager = await _manager(root)
    assert manager.managed_root is not None
    path = manager.managed_root / "feature" / "conflict"
    path.mkdir(parents=True)

    with pytest.raises(WorktreeError) as caught:
        await manager.create("feature/conflict")

    assert caught.value.code == "worktree_path_conflict"
    await manager.close()


async def test_branch_conflict_is_fail_closed(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    branch = worktree_branch_name("feature/branch-conflict")
    _git(root, "branch", branch)
    manager = await _manager(root)

    with pytest.raises(WorktreeError) as caught:
        await manager.create("feature/branch-conflict")

    assert caught.value.code == "worktree_branch_conflict"
    await manager.close()


async def test_initializer_diagnostics_are_persisted(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    manager = await _manager(
        root,
        config=WorktreeRuntimeConfig(
            local_config_files=(),
            copy_ignored=("tracked.txt",),
        ),
    )

    created = await manager.create("feature/diagnostic")

    assert [
        (item.stage, item.path, item.code)
        for item in created.record.initialization_diagnostics
    ] == [
        (
            "copy_ignored",
            "tracked.txt",
            "worktree_ignored_not_ignored",
        )
    ]
    recovered = await manager.create("feature/diagnostic")
    assert recovered.record.initialization_diagnostics == (
        created.record.initialization_diagnostics
    )
    await manager.delete("feature/diagnostic")
    await manager.close()


async def test_relative_hooks_path_is_stored_as_absolute(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _git(root, "config", "core.hooksPath", ".githooks")
    manager = await _manager(root)

    created = await manager.create("feature/hooks")

    configured = _git(
        created.record.path,
        "config",
        "--worktree",
        "--get",
        "core.hooksPath",
    )
    assert configured == str((root / ".githooks").resolve())
    await manager.delete("feature/hooks")
    await manager.close()


async def test_dirty_worktree_requires_confirmed_discard(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    manager = await _manager(root)
    created = await manager.create("feature/dirty")
    (created.record.path / "untracked.txt").write_text("dirty", encoding="utf-8")

    status = await manager.status("feature/dirty")
    assert status.dirty is True
    assert status.dirty_entry_count == 1
    with pytest.raises(WorktreeError) as caught:
        await manager.delete("feature/dirty")
    assert caught.value.code == "worktree_delete_unsafe"

    await manager.delete("feature/dirty", discard_confirmed=True)
    assert not created.record.path.exists()
    await manager.close()


async def test_commit_without_upstream_is_unpushed(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    manager = await _manager(root)
    created = await manager.create("feature/commit")
    (created.record.path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    _git(created.record.path, "add", "tracked.txt")
    _git(created.record.path, "commit", "-m", "worktree change")

    status = await manager.status("feature/commit")

    assert status.dirty is False
    assert status.upstream is None
    assert status.unpushed_commit_count is None
    assert status.has_unpushed is True
    assert status.deletion_safe is False
    await manager.delete("feature/commit", discard_confirmed=True)
    await manager.close()


async def test_upstream_ahead_count_blocks_safe_delete(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    remote = (tmp_path / "remote.git").resolve()
    remote.mkdir()
    _git(remote, "init", "--bare")
    _git(root, "remote", "add", "origin", str(remote))
    manager = await _manager(root)
    created = await manager.create("feature/upstream")
    _git(
        created.record.path,
        "push",
        "-u",
        "origin",
        created.record.branch,
    )
    (created.record.path / "tracked.txt").write_text("ahead\n", encoding="utf-8")
    _git(created.record.path, "add", "tracked.txt")
    _git(created.record.path, "commit", "-m", "ahead")

    status = await manager.status("feature/upstream")

    assert status.upstream == f"origin/{created.record.branch}"
    assert status.unpushed_commit_count == 1
    assert status.has_unpushed is True
    await manager.delete("feature/upstream", discard_confirmed=True)
    await manager.close()


async def test_active_owner_blocks_worker_delete(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    manager = await _manager(root)
    task_id = "a" * 32
    await manager.create(
        f"worker/{task_id}",
        kind="worker",
        owner_id=task_id,
    )
    await manager.claim_owner(task_id)

    with pytest.raises(WorktreeError) as caught:
        await manager.delete(f"worker/{task_id}")

    assert caught.value.code == "worktree_in_use"
    await manager.release_owner(task_id)
    await manager.delete(f"worker/{task_id}")
    await manager.close()


async def test_activate_blocks_delete_and_deactivate_returns_main(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    manager = await _manager(root)
    created = await manager.create("feature/active")

    entered = await manager.activate("feature/active")
    repeated = await manager.activate("feature/active")

    assert entered.target == created.record.path
    assert entered.restart_required is True
    assert repeated.restart_required is False
    assert manager.resume_target() == created.record.path
    with pytest.raises(WorktreeError) as caught:
        await manager.delete("feature/active")
    assert caught.value.code == "worktree_in_use"

    exited = await manager.deactivate()
    repeated_exit = await manager.deactivate()
    assert exited.target == root
    assert exited.restart_required is True
    assert repeated_exit.restart_required is False
    assert manager.resume_target() == root
    await manager.delete("feature/active")
    await manager.close()


async def test_cleanup_only_deletes_expired_safe_workers(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    current = [NOW]
    manager = await _manager(
        root,
        config=WorktreeRuntimeConfig(
            stale_after_hours=1,
            cleanup_interval_seconds=60,
            local_config_files=(),
        ),
        clock=lambda: current[0],
    )
    task_id = "b" * 32
    manual = await manager.create("feature/manual")
    worker = await manager.create(
        f"worker/{task_id}",
        kind="worker",
        owner_id=task_id,
    )
    current[0] = NOW + timedelta(hours=2)

    await manager.cleanup_once()

    assert manual.record.path.exists()
    assert not worker.record.path.exists()
    assert [item.name for item in manager.list_records()] == ["feature/manual"]
    await manager.delete("feature/manual")
    await manager.close()


async def test_cleanup_close_is_idempotent(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    manager = await _manager(root)
    manager.start_cleanup()

    first = await manager.close()
    second = await manager.close()

    assert first.cleanup_task_cancelled is True
    assert second.cleanup_task_cancelled is False


async def test_repository_unavailable_is_deferred(tmp_path: Path) -> None:
    class FailingGit:
        async def repository_identity(self, _cwd):
            raise WorktreeError(
                "worktree_repository_unavailable",
                "not a repository",
            )

    manager = await WorktreeManager.open(
        tmp_path.resolve(),
        WorktreeRuntimeConfig(),
        git=FailingGit(),  # type: ignore[arg-type]
    )

    assert manager.available is False
    with pytest.raises(WorktreeError) as caught:
        manager.list_records()
    assert caught.value.code == "worktree_repository_unavailable"
    await manager.close()


async def test_state_write_failure_rolls_back_new_worktree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _repository(tmp_path)
    manager = await _manager(root)
    assert manager.managed_root is not None
    expected_path = manager.managed_root / "feature" / "rollback"

    def fail_write(*_args, **_kwargs):
        raise WorktreeError("worktree_state_invalid", "write failed")

    monkeypatch.setattr(manager_module, "write_worktree_state", fail_write)

    with pytest.raises(WorktreeError) as caught:
        await manager.create("feature/rollback")

    assert caught.value.code == "worktree_state_invalid"
    assert not expected_path.exists()
    assert _git(root, "branch", "--list", "mewcode-wt-feature-rollback-*") == ""
    await manager.close()


async def test_remove_failure_keeps_record(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _repository(tmp_path)
    runner = GitRunner()
    manager = await WorktreeManager.open(
        root,
        WorktreeRuntimeConfig(local_config_files=()),
        git=runner,
        now=lambda: NOW,
    )
    created = await manager.create("feature/remove-failure")
    original_run = runner.run

    async def fail_remove(cwd, *arguments, **kwargs):
        if arguments[:2] == ("worktree", "remove"):
            raise WorktreeError("worktree_remove_failed", "remove failed")
        return await original_run(cwd, *arguments, **kwargs)

    monkeypatch.setattr(runner, "run", fail_remove)

    with pytest.raises(WorktreeError) as caught:
        await manager.delete("feature/remove-failure")

    assert caught.value.code == "worktree_remove_failed"
    assert created.record.path.exists()
    assert [item.name for item in manager.list_records()] == [
        "feature/remove-failure"
    ]
    await manager.close()
