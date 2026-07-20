from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from mewcode_agent.commands import CommandController, CommandMode, CommandRegistry
from mewcode_agent.workers import (
    WorkerCatalog,
    WorkerCatalogSnapshot,
    WorkerCommandManager,
    WorkerExecutionOutcome,
    WorkerExecutionSpec,
    WorkerManager,
    WorkerRoleDefinition,
    WorkerRuntimeConfig,
)


@dataclass
class FakeUI:
    messages: list[tuple[str, ...]] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    confirmation_result: bool = True

    async def show_system_message(self, lines: tuple[str, ...]) -> None:
        self.messages.append(lines)

    def refresh_status(self, state: str) -> None:
        self.statuses.append(state)

    async def request_confirmation(self, request):
        del request
        return self.confirmation_result

    async def send_user_message(self, message: str, *, mode: CommandMode) -> None:
        del message, mode

    def get_default_mode(self) -> CommandMode:
        return "execute"

    def set_default_mode(self, mode: CommandMode) -> None:
        del mode

    def clear_transcript(self) -> None:
        return None


def role(tmp_path: Path) -> WorkerRoleDefinition:
    return WorkerRoleDefinition(
        "example",
        "Example worker",
        None,
        ("spawn_worker",),
        "inherit",
        5,
        "inherit",
        "none",
        "Do it.",
        "project",
        tmp_path.resolve(),
        (tmp_path / "example.md").resolve(),
    )


async def setup(tmp_path: Path):
    catalog = WorkerCatalog(
        WorkerCatalogSnapshot((role(tmp_path),), (), WorkerRuntimeConfig())
    )

    async def runner(_spec, _usage):
        await asyncio.Event().wait()
        return WorkerExecutionOutcome("done", True)

    manager = WorkerManager(WorkerRuntimeConfig(), runner)
    commands = WorkerCommandManager(catalog, manager)
    registry = CommandRegistry()
    for command in commands.specs():
        registry.register(command)
    registry.freeze()
    ui = FakeUI()
    return CommandController(registry, ui), ui, manager


async def test_lists_roles_and_empty_tasks(tmp_path: Path) -> None:
    controller, ui, manager = await setup(tmp_path)

    roles = await controller.dispatch("/workers roles")
    tasks = await controller.dispatch("/workers")

    assert roles.success is True
    assert "example | Example worker" in ui.messages[0][0]
    assert tasks.success is True
    assert ui.messages[1] == ("当前没有 Worker 任务",)
    await manager.close()


async def test_shows_and_cancels_task(tmp_path: Path) -> None:
    controller, ui, manager = await setup(tmp_path)
    spec = WorkerExecutionSpec(
        "a" * 32,
        "session-a",
        "fork",
        "fork",
        "task",
        None,
        (),
        frozenset(),
        "provider-a",
        "model-a",
    )
    await manager.start(spec, background=True, transition="explicit")

    shown = await controller.dispatch(f"/worker show {'a' * 32}")
    cancelled = await controller.dispatch(f"/worker cancel {'a' * 32}")

    assert shown.success is True
    assert ui.messages[0][0] == f"task_id: {'a' * 32}"
    assert cancelled.success is True
    assert "已取消" in ui.messages[1][0] or "已经结束" in ui.messages[1][0]
    await manager.close()


async def test_worker_commands_reject_inexact_arguments(tmp_path: Path) -> None:
    controller, ui, manager = await setup(tmp_path)

    result = await controller.dispatch(f"/worker Show {'A' * 32}")

    assert result.success is False
    assert "command_usage_invalid" in ui.messages[-1][0]
    await manager.close()


async def test_cancel_requires_confirmation(tmp_path: Path) -> None:
    controller, ui, manager = await setup(tmp_path)
    ui.confirmation_result = False
    spec = WorkerExecutionSpec(
        "b" * 32,
        "session-a",
        "fork",
        "fork",
        "task",
        None,
        (),
        frozenset(),
        "provider-a",
        "model-a",
    )
    await manager.start(spec, background=True, transition="explicit")

    result = await controller.dispatch(f"/worker cancel {'b' * 32}")

    assert result.success is True
    assert (await manager.get("b" * 32)).state in ("starting", "running")
    assert ui.statuses[-1] == "已取消终止 Worker"
    await manager.close()
