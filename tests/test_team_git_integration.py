from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess

import pytest

from mewcode_agent.teams import (
    TeamBackendRequest,
    TeamBackendResult,
    TeamManager,
    TeamRuntimeConfig,
)
from mewcode_agent.workers import (
    WorkerCatalog,
    WorkerCatalogSnapshot,
    WorkerRoleDefinition,
    WorkerRuntimeConfig,
)
from mewcode_agent.worktrees import WorktreeManager, WorktreeRuntimeConfig


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.value:032x}"


class ResultBackend:
    def __init__(self) -> None:
        self.results: dict[str, TeamBackendResult] = {}

    async def start(self, request: TeamBackendRequest) -> TeamBackendResult:
        return self.results[request.task.task_id]

    async def cancel(self, _task_id: str) -> bool:
        return False

    async def close(self) -> None:
        return None


def _run(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.rstrip("\r\n")


def _repository(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("Git executable is unavailable")
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _run(root, "init")
    _run(root, "config", "user.name", "MewCode Tests")
    _run(root, "config", "user.email", "tests@example.invalid")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _run(root, "add", "base.txt")
    _run(root, "commit", "-m", "base")
    return root


def _catalog(tmp_path: Path) -> WorkerCatalog:
    definition = WorkerRoleDefinition(
        "implementer",
        "Implement tasks",
        None,
        (),
        "inherit",
        5,
        "inherit",
        "worktree",
        "Use exact evidence.",
        "project",
        tmp_path.resolve(),
        (tmp_path / "implementer.md").resolve(),
    )
    return WorkerCatalog(
        WorkerCatalogSnapshot(
            (definition,),
            (),
            WorkerRuntimeConfig(),
        )
    )


async def _wait_completed(manager: TeamManager, task_id: str) -> None:
    for _ in range(200):
        if (await manager.get_task(task_id)).status == "completed":
            return
        await asyncio.sleep(0)
    raise AssertionError("Team task did not complete")


async def test_real_git_task_and_main_merge_keep_all_worktrees(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    worktrees = await WorktreeManager.open(
        root,
        WorktreeRuntimeConfig(local_config_files=()),
        now=lambda: NOW,
    )
    backend = ResultBackend()
    manager = await TeamManager.open(
        root,
        TeamRuntimeConfig(),
        catalog=_catalog(tmp_path),
        backend=backend,
        worktree_manager=worktrees,
        now=lambda: NOW,
        id_factory=SequentialIds(),
    )
    team = await manager.create_team("alpha", (("build", "implementer"),))
    task = await manager.create_task("Feature", "Commit feature.txt.")
    await worktrees.claim_owner(task.task_id)
    worker = await worktrees.create(
        f"worker/{task.task_id}",
        kind="worker",
        owner_id=task.task_id,
    )
    try:
        (worker.record.path / "feature.txt").write_text(
            "implemented\n",
            encoding="utf-8",
        )
        _run(worker.record.path, "add", "feature.txt")
        _run(worker.record.path, "commit", "-m", "implement feature")
    finally:
        await worktrees.release_owner(task.task_id)
    task_head = _run(worker.record.path, "rev-parse", "HEAD")
    backend.results[task.task_id] = TeamBackendResult(
        "completed",
        "Feature implemented.",
        None,
        worker.record.path,
        True,
        "team_integration_pending",
        worker.record.branch,
        task_head,
    )
    manager.start()
    await _wait_completed(manager, task.task_id)

    integrated = await manager.integrate_task(task.task_id)
    preview = await manager.preview_main_merge()
    merged = await manager.merge_into_main(preview)

    assert integrated.status == "integrated"
    assert merged.state == "merged"
    assert (root / "feature.txt").read_text(encoding="utf-8") == "implemented\n"
    names = {item.name for item in worktrees.list_records()}
    assert names == {
        team.integration_worktree_name,
        f"worker/{task.task_id}",
    }
    assert _run(root, "status", "--porcelain=v1") == ""
    await manager.close()
    await worktrees.close()
