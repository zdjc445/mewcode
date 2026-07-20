"""Local slash commands for Team views and destructive lifecycle actions."""

from __future__ import annotations

from collections import Counter

from mewcode_agent.commands import (
    CommandDomainError,
    CommandInvocation,
    CommandSpec,
    CommandUI,
    CommandUsageError,
    ConfirmationRequest,
)
from mewcode_agent.teams.manager import TeamManager
from mewcode_agent.teams.models import TeamError, TeamRecord


class TeamCommandManager:
    def __init__(self, manager: TeamManager) -> None:
        if not isinstance(manager, TeamManager):
            raise ValueError("manager 类型无效")
        self._manager = manager

    def specs(self) -> tuple[CommandSpec, CommandSpec]:
        return (
            CommandSpec(
                "teams",
                (),
                "列出当前项目持久 Team",
                "/teams",
                "local",
                "workflow",
                "不接受参数",
                self._teams,
            ),
            CommandSpec(
                "team",
                (),
                "查看、暂停、恢复、关闭或合并当前 Team",
                "/team <show|pause|resume|close|merge> [team_id|--into-main]",
                "local",
                "workflow",
                "子命令、team ID 与 --into-main 必须精确匹配",
                self._team,
            ),
        )

    async def _teams(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        if invocation.arguments:
            raise CommandUsageError
        try:
            teams = await self._manager.list_teams()
        except TeamError as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        lines = tuple(self._list_line(team) for team in teams)
        await ui.show_system_message(lines or ("当前项目没有持久 Team",))
        ui.refresh_status(f"已显示 {len(teams)} 个 Team")

    async def _team(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        parts = invocation.arguments.split(" ") if invocation.arguments else []
        if not parts:
            raise CommandUsageError
        action = parts[0]
        try:
            if action == "show" and len(parts) in (1, 2):
                if len(parts) == 2 and not _valid_team_id(parts[1]):
                    raise CommandUsageError
                team = await self._manager.get_team(
                    None if len(parts) == 1 else parts[1]
                )
                await ui.show_system_message(self._detail_lines(team))
                ui.refresh_status(f"已显示 Team：{team.team_id}")
                return
            if action == "pause" and len(parts) == 1:
                team = await self._manager.pause()
                await ui.show_system_message((f"Team {team.team_id} 已暂停",))
                ui.refresh_status("Team 已暂停")
                return
            if action == "resume" and len(parts) == 1:
                team = await self._manager.resume()
                await ui.show_system_message((f"Team {team.team_id} 已恢复",))
                ui.refresh_status("Team 已恢复")
                return
            if action == "close" and len(parts) == 1:
                team = await self._manager.get_team()
                counts = Counter(task.status for task in team.tasks)
                confirmed = await ui.request_confirmation(
                    ConfirmationRequest(
                        "team.close",
                        "关闭 Team 并取消运行中的成员任务",
                        (
                            ("team", team.name),
                            ("team ID", team.team_id),
                            ("running tasks", str(counts.get("running", 0))),
                            ("persistent data", "保留"),
                            ("worktrees", "保留"),
                        ),
                        True,
                    )
                )
                if not confirmed:
                    ui.refresh_status("已取消关闭 Team")
                    return
                closed = await self._manager.close_team()
                await ui.show_system_message((f"Team {closed.team_id} 已关闭",))
                ui.refresh_status("Team 已关闭")
                return
            if action == "merge" and parts == ["merge", "--into-main"]:
                preview = await self._manager.preview_main_merge()
                if preview.main_dirty or preview.integration_dirty:
                    raise TeamError(
                        "team_integration_unsafe",
                        "主工作树或 integration worktree 不干净",
                    )
                counts = ", ".join(
                    f"{status}={count}"
                    for status, count in preview.task_counts
                )
                confirmed = await ui.request_confirmation(
                    ConfirmationRequest(
                        "team.merge.into-main",
                        "更新主工作树并可能触发 Git hooks",
                        (
                            ("team", preview.team_name),
                            ("team ID", preview.team_id),
                            ("main path", str(preview.main_path)),
                            ("integration path", str(preview.integration_path)),
                            ("main HEAD", preview.main_head),
                            ("integration HEAD", preview.integration_head),
                            ("tasks", counts),
                            ("push", "不会执行"),
                            ("cleanup", "不会执行"),
                        ),
                        True,
                    )
                )
                if not confirmed:
                    ui.refresh_status("已取消合并 Team")
                    return
                merged = await self._manager.merge_into_main(preview)
                await ui.show_system_message(
                    (f"Team {merged.team_id} 已合入主工作树",)
                )
                ui.refresh_status("Team 合并完成")
                return
        except CommandUsageError:
            raise
        except TeamError as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        raise CommandUsageError

    @staticmethod
    def _list_line(team: TeamRecord) -> str:
        counts = Counter(task.status for task in team.tasks)
        return " | ".join(
            (
                team.team_id,
                team.name,
                team.state,
                f"members={len(team.members)}",
                f"tasks={len(team.tasks)}",
                f"running={counts.get('running', 0)}",
                f"failed={counts.get('failed', 0)}",
                f"integrated={counts.get('integrated', 0)}",
            )
        )
    @staticmethod
    def _detail_lines(team: TeamRecord) -> tuple[str, ...]:
        counts = Counter(task.status for task in team.tasks)
        return (
            f"team_id: {team.team_id}",
            f"name: {team.name}",
            f"state: {team.state}",
            f"base_head: {team.base_head}",
            f"integration_worktree: {team.integration_worktree_name}",
            f"created_at: {team.created_at}",
            f"updated_at: {team.updated_at}",
            "members:",
            *(
                (
                    f"- {member.name} | role={member.role} | "
                    f"state={member.state} | "
                    f"task={member.current_task_id or '-'}"
                )
                for member in team.members
            ),
            "task_counts: "
            + ", ".join(
                f"{status}={counts.get(status, 0)}"
                for status in (
                    "blocked",
                    "pending",
                    "running",
                    "completed",
                    "integrated",
                    "failed",
                    "cancelled",
                )
            ),
        )


def _valid_team_id(value: str) -> bool:
    return (
        len(value) == 32
        and value.startswith("t")
        and all(character in "0123456789abcdef" for character in value[1:])
    )
