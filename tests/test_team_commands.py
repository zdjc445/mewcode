from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from mewcode_agent.commands import (
    CommandController,
    CommandMode,
    CommandRegistry,
    ConfirmationRequest,
)
from mewcode_agent.teams import (
    TeamCommandManager,
    TeamMainMergePreview,
    TeamManager,
    TeamMemberRecord,
    TeamRecord,
)


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc).isoformat()
TEAM_ID = "t" + "1" * 31
HEAD = "a" * 40


def _team(state: str = "active") -> TeamRecord:
    member = TeamMemberRecord(
        "2" * 32,
        "build",
        "implementer",
        "in_process",
        "idle",
        None,
        0,
        NOW,
        NOW,
    )
    return TeamRecord(
        TEAM_ID,
        "alpha",
        state,
        HEAD,
        f"team/{TEAM_ID}/integration",
        0,
        NOW,
        NOW,
        (member,),
        (),
        (),
    )


class StubTeamManager(TeamManager):
    def __init__(self, root: Path) -> None:
        self.team = _team()
        self.root = root
        self.merge_calls = 0

    async def list_teams(self):
        return (self.team,)

    async def get_team(self, team_id=None):
        assert team_id in (None, TEAM_ID)
        return self.team

    async def pause(self):
        self.team = replace(self.team, state="paused")
        return self.team

    async def resume(self):
        self.team = replace(self.team, state="active")
        return self.team

    async def close_team(self):
        self.team = replace(self.team, state="closed")
        return self.team

    async def preview_main_merge(self):
        return TeamMainMergePreview(
            TEAM_ID,
            "alpha",
            self.root,
            (self.root / "integration").resolve(),
            HEAD,
            "b" * 40,
            (("integrated", 1),),
            False,
            False,
        )

    async def merge_into_main(self, _preview):
        self.merge_calls += 1
        self.team = replace(self.team, state="merged")
        return self.team


class UI:
    def __init__(self) -> None:
        self.messages: list[tuple[str, ...]] = []
        self.statuses: list[str] = []
        self.confirmations: list[ConfirmationRequest] = []
        self.confirmed = True

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
        raise AssertionError(target)


def _controller(root: Path):
    manager = StubTeamManager(root)
    registry = CommandRegistry()
    for spec in TeamCommandManager(manager).specs():
        registry.register(spec)
    registry.freeze()
    ui = UI()
    return CommandController(registry, ui), ui, manager


async def test_team_list_show_pause_and_resume(tmp_path: Path) -> None:
    controller, ui, manager = _controller(tmp_path.resolve())

    assert (await controller.dispatch("/teams")).success is True
    assert (await controller.dispatch(f"/team show {TEAM_ID}")).success is True
    assert (await controller.dispatch("/team pause")).success is True
    assert manager.team.state == "paused"
    assert (await controller.dispatch("/team resume")).success is True
    assert manager.team.state == "active"
    assert any("alpha" in line for line in ui.messages[0])


async def test_team_close_and_main_merge_require_confirmation(tmp_path: Path) -> None:
    controller, ui, manager = _controller(tmp_path.resolve())
    ui.confirmed = False

    cancelled_close = await controller.dispatch("/team close")
    cancelled_merge = await controller.dispatch("/team merge --into-main")

    assert cancelled_close.success is True
    assert cancelled_merge.success is True
    assert manager.team.state == "active"
    assert manager.merge_calls == 0
    assert [item.action_id for item in ui.confirmations] == [
        "team.close",
        "team.merge.into-main",
    ]

    ui.confirmed = True
    merged = await controller.dispatch("/team merge --into-main")
    assert merged.success is True
    assert manager.merge_calls == 1
    assert ui.confirmations[-1].destructive is True


async def test_team_commands_reject_inexact_arguments(tmp_path: Path) -> None:
    controller, ui, _ = _controller(tmp_path.resolve())

    for command in (
        "/teams extra",
        "/team",
        "/team Pause",
        "/team show invalid",
        "/team merge --into-Main",
        "/team close extra",
    ):
        result = await controller.dispatch(command)
        assert result.success is False
        assert "command_usage_invalid" in ui.messages[-1][0]
