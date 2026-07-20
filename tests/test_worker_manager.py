from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from mewcode_agent.workers import (
    WorkerError,
    WorkerExecutionOutcome,
    WorkerExecutionSpec,
    WorkerManager,
    WorkerRuntimeConfig,
    WorkerWorkspaceSnapshot,
)


def spec(task_id: str, *, session_id: str = "session-a") -> WorkerExecutionSpec:
    return WorkerExecutionSpec(
        task_id,
        session_id,
        "fork",
        "fork",
        "task",
        None,
        (),
        frozenset(),
        "provider-a",
        "model-a",
    )


FIXED_NOW = datetime.fromisoformat("2026-07-21T12:00:00+08:00")


async def test_foreground_completion_returns_terminal_without_notification() -> None:
    async def runner(_spec, _usage):
        return WorkerExecutionOutcome("done", True)

    manager = WorkerManager(
        WorkerRuntimeConfig(),
        runner,
        now=lambda: FIXED_NOW,
    )
    started = await manager.start(
        spec("a" * 32),
        background=False,
        transition=None,
    )

    completed = await manager.wait_foreground(started.task_id)

    assert completed.state == "completed"
    assert completed.mode == "foreground"
    assert completed.result == "done"
    assert await manager.take_notifications("session-a") == ()
    await manager.close()


async def test_foreground_timeout_detaches_same_task_and_notifies_later() -> None:
    release = asyncio.Event()

    async def runner(_spec, _usage):
        await release.wait()
        return WorkerExecutionOutcome("later", True)

    manager = WorkerManager(
        WorkerRuntimeConfig(foreground_timeout_seconds=0.01),
        runner,
        now=lambda: FIXED_NOW,
    )
    started = await manager.start(
        spec("b" * 32),
        background=False,
        transition=None,
    )

    detached = await manager.wait_foreground(started.task_id)

    assert detached.state == "running"
    assert detached.mode == "background"
    assert detached.transition == "timeout"
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    completed = await manager.get(started.task_id)
    assert completed.state == "completed"
    notifications = await manager.take_notifications("session-a")
    assert len(notifications) == 1
    assert notifications[0].result == "later"
    assert await manager.take_notifications("session-a") == ()
    await manager.close()


async def test_explicit_background_truncates_notification_only() -> None:
    result = "a" * 9000

    async def runner(_spec, _usage):
        return WorkerExecutionOutcome(result, True)

    manager = WorkerManager(WorkerRuntimeConfig(), runner)
    await manager.start(
        spec("c" * 32),
        background=True,
        transition="explicit",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    snapshot = await manager.get("c" * 32)
    notification = (await manager.take_notifications("session-a"))[0]
    assert snapshot.result == result
    assert len(notification.result) <= 8000
    assert "[worker result truncated]" in notification.result
    await manager.close()


async def test_workspace_state_flows_to_snapshot_and_notification() -> None:
    workspace = WorkerWorkspaceSnapshot(
        str(Path.cwd().resolve()),
        True,
        "worktree_dirty",
    )

    async def runner(_spec, _usage):
        return WorkerExecutionOutcome("done", True)

    manager = WorkerManager(
        WorkerRuntimeConfig(),
        runner,
        workspace_provider=lambda _task_id: workspace,
    )
    await manager.start(
        spec("9" * 32),
        background=True,
        transition="explicit",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    snapshot = await manager.get("9" * 32)
    notification = (await manager.take_notifications("session-a"))[0]

    assert snapshot.workspace == workspace
    assert notification.workspace == workspace
    assert notification.to_dict()["workspace"] == {
        "path": workspace.path,
        "preserved": True,
        "reason": "worktree_dirty",
    }
    await manager.close()


async def test_capacity_and_single_foreground_are_enforced() -> None:
    release = asyncio.Event()

    async def runner(_spec, _usage):
        await release.wait()
        return WorkerExecutionOutcome("done", True)

    manager = WorkerManager(
        WorkerRuntimeConfig(max_concurrency=2),
        runner,
    )
    await manager.start(
        spec("d" * 32),
        background=False,
        transition=None,
    )
    with pytest.raises(WorkerError) as caught:
        await manager.start(
            spec("e" * 32),
            background=False,
            transition=None,
        )
    assert caught.value.code == "worker_capacity_reached"
    await manager.start(
        spec("f" * 32),
        background=True,
        transition="explicit",
    )
    with pytest.raises(WorkerError) as caught:
        await manager.start(
            spec("1" * 32),
            background=True,
            transition="explicit",
        )
    assert caught.value.code == "worker_capacity_reached"
    release.set()
    await manager.close()


async def test_cancel_marks_task_terminal_even_before_runner_starts() -> None:
    async def runner(_spec, _usage):
        await asyncio.Event().wait()
        return WorkerExecutionOutcome("never", True)

    manager = WorkerManager(WorkerRuntimeConfig(), runner)
    await manager.start(
        spec("2" * 32),
        background=True,
        transition="explicit",
    )

    assert await manager.cancel("2" * 32) is True
    snapshot = await manager.get("2" * 32)
    assert snapshot.state == "cancelled"
    assert snapshot.error_code == "worker_cancelled"
    await manager.close()


async def test_close_is_idempotent_and_clears_notifications() -> None:
    release = asyncio.Event()

    async def runner(worker_spec, _usage):
        if worker_spec.task_id == "3" * 32:
            return WorkerExecutionOutcome("done", True)
        await release.wait()
        return WorkerExecutionOutcome("late", True)

    manager = WorkerManager(WorkerRuntimeConfig(), runner)
    await manager.start(
        spec("3" * 32),
        background=True,
        transition="explicit",
    )
    await manager.start(
        spec("4" * 32),
        background=True,
        transition="explicit",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    first = await manager.close()
    second = await manager.close()

    assert first == second
    assert first.active_tasks == 1
    assert first.cleared_notifications >= 1
    assert (await manager.get("4" * 32)).state == "cancelled"
    assert await manager.take_notifications("session-a") == ()
