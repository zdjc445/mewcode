from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.history import ConversationHistory
from mewcode_agent.tools import ToolExecutionError, ToolRegistry
from mewcode_agent.workers import (
    SpawnWorkerTool,
    WorkerCatalog,
    WorkerCatalogSnapshot,
    WorkerExecutionOutcome,
    WorkerManager,
    WorkerRoleDefinition,
    WorkerRuntimeConfig,
)


def role(tmp_path: Path, *, isolation: str = "none") -> WorkerRoleDefinition:
    return WorkerRoleDefinition(
        "example",
        "Example role",
        None,
        ("spawn_worker",),
        "inherit",
        5,
        "inherit",
        isolation,  # type: ignore[arg-type]
        "Do the task.",
        "project",
        tmp_path.resolve(),
        (tmp_path / "example.md").resolve(),
    )


def setup_tool(
    tmp_path: Path,
    *,
    definition: WorkerRoleDefinition | None = None,
    timeout: float = 15,
) -> tuple[SpawnWorkerTool, WorkerManager]:
    definitions = () if definition is None else (definition,)
    runtime = WorkerRuntimeConfig(
        foreground_timeout_seconds=timeout,
        background_allowed_tools=("read_file",),
    )
    catalog = WorkerCatalog(WorkerCatalogSnapshot(definitions, (), runtime))

    async def runner(_spec, _usage):
        return WorkerExecutionOutcome("done", True)

    manager = WorkerManager(runtime, runner)
    registry = ToolRegistry()
    tool = SpawnWorkerTool(
        catalog=catalog,
        manager=manager,
        registry=registry,
        main_history=ConversationHistory(),
        session_id_provider=lambda: "session-a",
        parent_visible_tools=lambda: frozenset(registry.tool_names()),
        parent_provider_id="provider-a",
        provider_models={"provider-a": "model-a"},
    )
    registry.register(tool)
    return tool, manager


async def test_fork_is_forced_to_background(tmp_path: Path) -> None:
    tool, manager = setup_tool(tmp_path)

    result = await tool.execute({"task": "inspect", "background": False})

    assert result["status"] == "running"
    assert result["mode"] == "background"
    assert result["type"] == "fork"
    assert result["transition"] == "fork_forced"
    await manager.close()


async def test_definition_returns_foreground_result(tmp_path: Path) -> None:
    tool, manager = setup_tool(tmp_path, definition=role(tmp_path))

    result = await tool.execute({"task": "inspect", "type": "example"})

    assert result["status"] == "completed"
    assert result["mode"] == "foreground"
    assert result["type"] == "example"
    assert result["result"] == "done"
    assert result["workspace"] is None
    await manager.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"task": ""},
        {"task": "x", "type": "Example"},
        {"task": "x", "role": "example"},
        {"task": "x", "background": "yes"},
    ],
)
async def test_rejects_invalid_exact_arguments(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    tool, manager = setup_tool(tmp_path, definition=role(tmp_path))

    with pytest.raises(ToolExecutionError) as caught:
        await tool.execute(arguments)

    assert caught.value.code == "invalid_arguments"
    await manager.close()


async def test_unknown_type_has_stable_error(tmp_path: Path) -> None:
    tool, manager = setup_tool(tmp_path)

    with pytest.raises(ToolExecutionError) as caught:
        await tool.execute({"task": "x", "type": "missing"})

    assert caught.value.code == "worker_type_not_found"
    await manager.close()


async def test_worktree_role_is_dispatched_to_executor(
    tmp_path: Path,
) -> None:
    tool, manager = setup_tool(
        tmp_path,
        definition=role(tmp_path, isolation="worktree"),
    )

    result = await tool.execute({"task": "x", "type": "example"})

    assert result["status"] == "completed"
    assert result["type"] == "example"
    await manager.close()
