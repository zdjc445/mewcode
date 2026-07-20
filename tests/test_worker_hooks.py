from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mewcode_agent.compaction import SummarySections
from mewcode_agent.history import ConversationHistory
from mewcode_agent.tools import Tool, ToolRegistry
from mewcode_agent.workers import (
    HookSubagentLauncher,
    WorkerCatalog,
    WorkerCatalogSnapshot,
    WorkerExecutionOutcome,
    WorkerExecutionSpec,
    WorkerManager,
    WorkerRoleDefinition,
    WorkerRuntimeConfig,
)


class ReadTool(Tool):
    name = "read_file"
    description = "read"
    category = "read"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments: dict[str, Any]) -> Any:
        return arguments


def general_role(tmp_path: Path) -> WorkerRoleDefinition:
    return WorkerRoleDefinition(
        "general",
        "General",
        None,
        ("spawn_worker",),
        "inherit",
        5,
        "inherit",
        "none",
        "Do the task.",
        "project",
        tmp_path.resolve(),
        (tmp_path / "general.md").resolve(),
    )


async def setup(tmp_path: Path):
    runtime = WorkerRuntimeConfig(background_allowed_tools=("read_file",))
    catalog = WorkerCatalog(
        WorkerCatalogSnapshot((general_role(tmp_path),), (), runtime)
    )
    captured: list[WorkerExecutionSpec] = []

    async def runner(spec, _usage):
        captured.append(spec)
        return WorkerExecutionOutcome("done", True)

    manager = WorkerManager(runtime, runner)
    registry = ToolRegistry()
    registry.register(ReadTool())
    history = ConversationHistory()
    sections = SummarySections(
        ("request",),
        (),
        (),
        (),
        (),
        (),
        (),
        ("next",),
    )

    class Summarizer:
        async def summarize(self, **_kwargs):
            return SimpleNamespace(sections=sections)

    launcher = HookSubagentLauncher(
        catalog=catalog,
        manager=manager,
        registry=registry,
        main_history=history,
        session_id_provider=lambda: "session-a",
        parent_visible_tools=lambda: frozenset({"read_file"}),
        parent_provider_id="provider-a",
        provider_models={"provider-a": "model-a"},
        summarizer=Summarizer(),  # type: ignore[arg-type]
    )
    return launcher, manager, history, captured


async def test_none_context_uses_general_definition_in_background(
    tmp_path: Path,
) -> None:
    launcher, manager, _history, captured = await setup(tmp_path)

    await launcher.launch("inspect", "none")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    spec = captured[0]
    assert spec.kind == "hook"
    assert spec.worker_type == "general"
    assert spec.definition is not None
    assert spec.parent_history == ()
    assert spec.visible_tools == frozenset({"read_file"})
    snapshot = (await manager.list())[0]
    assert snapshot.mode == "background"
    await manager.close()


async def test_recent_context_uses_fork_and_last_twelve_messages(
    tmp_path: Path,
) -> None:
    launcher, manager, history, captured = await setup(tmp_path)
    for index in range(13):
        history.add_user(f"message-{index}")

    await launcher.launch("inspect", "recent")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    spec = captured[0]
    assert spec.worker_type == "fork"
    assert spec.definition is None
    assert [item.content for item in spec.parent_history] == [
        f"message-{index}" for index in range(1, 13)
    ]
    await manager.close()


async def test_summary_context_uses_structured_summary_boundary(
    tmp_path: Path,
) -> None:
    launcher, manager, history, captured = await setup(tmp_path)
    history.add_user("original exact user message")

    await launcher.launch("inspect", "summary")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    spec = captured[0]
    assert spec.definition is not None
    assert len(spec.parent_history) == 1
    summary = spec.parent_history[0].content
    assert '"primary_requests":["request"]' in summary
    assert '"content":"original exact user message"' in summary
    assert "上下文压缩边界" in summary
    await manager.close()
