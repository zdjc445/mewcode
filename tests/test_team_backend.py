from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from mewcode_agent.models import ChatMessage
from mewcode_agent.teams import (
    InProcessTeamBackend,
    TeamBackendRequest,
    TeamDependencyResult,
    TeamError,
    TeamMailboxMessage,
    TeamMemberRecord,
    TeamTaskRecord,
)
from mewcode_agent.tools import Tool, ToolRegistry
from mewcode_agent.workers import (
    WorkerCatalog,
    WorkerCatalogSnapshot,
    WorkerExecutionOutcome,
    WorkerManager,
    WorkerRoleDefinition,
    WorkerRuntimeConfig,
    WorkerWorkspaceSnapshot,
)
from mewcode_agent.worktrees import WorktreeStatus, worktree_branch_name


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
TEAM_ID = "t" + "1" * 31
MEMBER_ID = "2" * 32
TASK_ID = "3" * 32
DEPENDENCY_ID = "4" * 32
HEAD = "a" * 40


class NamedTool(Tool):
    description = "test"
    category = "read"
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, arguments: dict[str, Any]) -> object:
        return arguments


class RecordingWorktrees:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def status(self, name: str) -> WorktreeStatus:
        self.names.append(name)
        return WorktreeStatus(True, HEAD, False, 0, None, 1, True, False, None)


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
    runtime = WorkerRuntimeConfig(
        background_allowed_tools=("read_file", "team_status")
    )
    return WorkerCatalog(WorkerCatalogSnapshot((definition,), (), runtime))


def _request() -> TeamBackendRequest:
    member = TeamMemberRecord(
        MEMBER_ID,
        "builder",
        "implementer",
        "in_process",
        "running",
        TASK_ID,
        0,
        NOW.isoformat(),
        NOW.isoformat(),
    )
    task = TeamTaskRecord(
        TASK_ID,
        "Implement cache",
        "Keep the original instructions exact.",
        "running",
        "builder",
        (DEPENDENCY_ID,),
        NOW.isoformat(),
        NOW.isoformat(),
        started_at=NOW.isoformat(),
    )
    dependency = TeamDependencyResult(
        DEPENDENCY_ID,
        "Prepare API",
        "completed",
        "API prepared",
    )
    message = TeamMailboxMessage(
        "5" * 32,
        TEAM_ID,
        "lead",
        "builder",
        "message",
        NOW.isoformat(),
        "Check invalidation first.",
    )
    return TeamBackendRequest(
        TEAM_ID,
        member,
        task,
        (dependency,),
        (message,),
        (ChatMessage("user", "previous"), ChatMessage("assistant", "done")),
    )


async def test_in_process_backend_wraps_worker_and_filters_team_tools(
    tmp_path: Path,
) -> None:
    captured = []
    workspace = WorkerWorkspaceSnapshot(
        str((tmp_path / "worker").resolve()),
        True,
        "team_integration_pending",
    )

    async def runner(spec, _usage):
        captured.append(spec)
        return WorkerExecutionOutcome("implemented", True)

    manager = WorkerManager(
        WorkerRuntimeConfig(background_allowed_tools=("read_file", "team_status")),
        runner,
        workspace_provider=lambda _task_id: workspace,
    )
    registry = ToolRegistry()
    registry.register(NamedTool("read_file"))
    registry.register(NamedTool("team_status"))
    registry.register(NamedTool("spawn_worker"))
    worktrees = RecordingWorktrees()
    backend = InProcessTeamBackend(
        catalog=_catalog(tmp_path),
        manager=manager,
        registry=registry,
        parent_provider_id="provider-a",
        provider_models={"provider-a": "model-a"},
        worktree_manager=worktrees,  # type: ignore[arg-type]
    )

    result = await backend.start(_request())

    assert result.state == "completed"
    assert result.result == "implemented"
    assert result.head == HEAD
    assert result.branch == worktree_branch_name(f"worker/{TASK_ID}")
    assert worktrees.names == [f"worker/{TASK_ID}"]
    assert len(captured) == 1
    spec = captured[0]
    assert spec.visible_tools == frozenset({"read_file"})
    assert spec.preserve_workspace is True
    assert spec.parent_history == _request().history
    assert "API prepared" in spec.task
    assert "Check invalidation first." in spec.task
    assert "Keep the original instructions exact." in spec.task
    assert await manager.take_notifications(spec.session_id) == ()
    await backend.close()
    await manager.close()


async def test_backend_rejects_success_without_workspace(tmp_path: Path) -> None:
    async def runner(_spec, _usage):
        return WorkerExecutionOutcome("implemented", True)

    manager = WorkerManager(WorkerRuntimeConfig(), runner)
    backend = InProcessTeamBackend(
        catalog=_catalog(tmp_path),
        manager=manager,
        registry=ToolRegistry(),
        parent_provider_id="provider-a",
        provider_models={"provider-a": "model-a"},
        worktree_manager=RecordingWorktrees(),  # type: ignore[arg-type]
    )

    with pytest.raises(TeamError) as caught:
        await backend.start(_request())

    assert caught.value.code == "team_backend_contract_failed"
    await backend.close()
    await manager.close()
