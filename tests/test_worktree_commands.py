from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from mewcode_agent.commands import (
    CommandController,
    CommandMode,
    CommandRegistry,
    ConfirmationRequest,
)
from mewcode_agent.worktrees import (
    WorktreeCommandManager,
    WorktreeManager,
    WorktreeRuntimeConfig,
)


class _UI:
    def __init__(self) -> None:
        self.messages: list[tuple[str, ...]] = []
        self.statuses: list[str] = []
        self.confirmations: list[ConfirmationRequest] = []
        self.confirmed = True
        self.restart_targets: list[Path] = []

    async def show_system_message(self, lines: tuple[str, ...]) -> None:
        self.messages.append(lines)

    async def request_confirmation(self, request: ConfirmationRequest) -> bool:
        self.confirmations.append(request)
        return self.confirmed

    async def send_user_message(self, message: str, *, mode: CommandMode) -> None:
        raise AssertionError((message, mode))

    def get_default_mode(self) -> CommandMode:
        return "execute"

    def set_default_mode(self, mode: CommandMode) -> None:
        del mode

    def clear_transcript(self) -> None:
        return None

    def refresh_status(self, state: str) -> None:
        self.statuses.append(state)

    def request_workspace_restart(self, target: Path) -> None:
        self.restart_targets.append(target)


def _git(root: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git executable is unavailable")
    subprocess.run(
        [executable, "-C", str(root), *arguments],
        check=True,
        capture_output=True,
    )


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


async def _controller(
    root: Path,
) -> tuple[CommandController, _UI, WorktreeManager]:
    manager = await WorktreeManager.open(
        root,
        WorktreeRuntimeConfig(local_config_files=()),
    )
    registry = CommandRegistry()
    for spec in WorktreeCommandManager(manager).specs():
        registry.register(spec)
    registry.freeze()
    ui = _UI()
    return CommandController(registry, ui), ui, manager


async def test_create_list_status_enter_and_exit_commands(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    controller, ui, manager = await _controller(root)

    created = await controller.dispatch("/worktree create feature/cache")
    listed = await controller.dispatch("/worktrees")
    status = await controller.dispatch("/worktree status feature/cache")
    entered = await controller.dispatch("/worktree enter feature/cache")

    assert created.success is True
    assert listed.success is True
    assert status.success is True
    assert entered.success is True
    record = manager.list_records()[0]
    assert ui.restart_targets == [record.path]
    assert any("feature/cache" in line for line in ui.messages[1])

    exited = await controller.dispatch("/worktree exit")
    assert exited.success is True
    assert ui.restart_targets == [record.path]
    await manager.delete("feature/cache")
    await manager.close()


async def test_discard_requires_confirmation_and_reports_summary(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    controller, ui, manager = await _controller(root)
    await controller.dispatch("/worktree create feature/dirty")
    record = manager.list_records()[0]
    (record.path / "untracked.txt").write_text("dirty", encoding="utf-8")
    ui.confirmed = False

    cancelled = await controller.dispatch(
        "/worktree delete feature/dirty --discard"
    )

    assert cancelled.success is True
    assert record.path.exists()
    assert len(ui.confirmations) == 1
    fields = dict(ui.confirmations[0].fields)
    assert fields["dirty"] == "true"
    assert fields["dirty entries"] == "1"
    assert fields["recovery"] == "不可恢复"

    ui.confirmed = True
    deleted = await controller.dispatch(
        "/worktree delete feature/dirty --discard"
    )
    assert deleted.success is True
    assert not record.path.exists()
    await manager.close()


@pytest.mark.parametrize(
    "command",
    [
        "/worktrees extra",
        "/worktree",
        "/worktree Create feature/x",
        "/worktree create  feature/x",
        "/worktree exit extra",
        "/worktree delete feature/x --force",
    ],
)
async def test_worktree_command_arguments_are_exact(
    tmp_path: Path,
    command: str,
) -> None:
    root = _repository(tmp_path)
    controller, ui, manager = await _controller(root)

    result = await controller.dispatch(command)

    assert result.consumed is True
    assert result.success is False
    assert "command_usage_invalid" in ui.messages[-1][0]
    await manager.close()


async def test_status_without_managed_current_worktree_is_stable_error(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    controller, ui, manager = await _controller(root)

    result = await controller.dispatch("/worktree status")

    assert result.success is False
    assert "worktree_not_found" in ui.messages[-1][0]
    await manager.close()
