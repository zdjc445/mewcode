"""Local slash commands for managed worktree lifecycle."""

from __future__ import annotations

from mewcode_agent.commands import (
    CommandDomainError,
    CommandInvocation,
    CommandSpec,
    CommandUI,
    CommandUsageError,
    ConfirmationRequest,
)
from mewcode_agent.worktrees.manager import WorktreeManager
from mewcode_agent.worktrees.models import (
    WorktreeError,
    WorktreeRecord,
    WorktreeStatus,
)


class WorktreeCommandManager:
    def __init__(self, manager: WorktreeManager) -> None:
        if not isinstance(manager, WorktreeManager):
            raise ValueError("manager 类型无效")
        self._manager = manager

    def specs(self) -> tuple[CommandSpec, CommandSpec]:
        return (
            CommandSpec(
                "worktrees",
                (),
                "列出受管 Git worktree",
                "/worktrees",
                "local",
                "workflow",
                "不接受参数",
                self._worktrees,
            ),
            CommandSpec(
                "worktree",
                (),
                "创建、切换、查看或删除受管 Git worktree",
                "/worktree <create|enter|exit|status|delete> [name] [--discard]",
                "local",
                "workflow",
                "子命令、name 和 --discard 必须精确匹配",
                self._worktree,
            ),
        )

    async def _worktrees(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        if invocation.arguments:
            raise CommandUsageError
        try:
            records = self._manager.list_records()
            active_name = self._manager.active_name
            lines: list[str] = []
            for record in records:
                status = await self._manager.status(record.name)
                lines.append(
                    self._list_line(
                        record,
                        status,
                        active=record.name == active_name,
                    )
                )
        except WorktreeError as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        await ui.show_system_message(tuple(lines) or ("当前没有受管 Worktree",))
        ui.refresh_status(f"已显示 {len(lines)} 个 Worktree")

    async def _worktree(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        parts = invocation.arguments.split(" ") if invocation.arguments else []
        if not parts:
            raise CommandUsageError
        action = parts[0]
        try:
            if action == "create" and len(parts) == 2:
                result = await self._manager.create(parts[1])
                await ui.show_system_message(
                    (
                        (
                            "已恢复 Worktree" if result.recovered else "已创建 Worktree"
                        ),
                        f"name: {result.record.name}",
                        f"path: {result.record.path}",
                        f"branch: {result.record.branch}",
                    )
                )
                ui.refresh_status("Worktree 创建完成")
                return
            if action == "enter" and len(parts) == 2:
                result = await self._manager.activate(parts[1])
                await ui.show_system_message(
                    (
                        f"Worktree target: {result.target}",
                        (
                            "正在重建运行时"
                            if result.restart_required
                            else "已经位于目标 Worktree"
                        ),
                    )
                )
                ui.refresh_status("Worktree enter 完成")
                if result.restart_required:
                    ui.request_workspace_restart(result.target)
                return
            if action == "exit" and len(parts) == 1:
                result = await self._manager.deactivate()
                await ui.show_system_message(
                    (
                        f"Main worktree target: {result.target}",
                        (
                            "正在重建运行时"
                            if result.restart_required
                            else "已经位于主 Worktree"
                        ),
                    )
                )
                ui.refresh_status("Worktree exit 完成")
                if result.restart_required:
                    ui.request_workspace_restart(result.target)
                return
            if action == "status" and len(parts) in (1, 2):
                name = parts[1] if len(parts) == 2 else self._manager.current_name
                if name is None:
                    raise WorktreeError(
                        "worktree_not_found",
                        "当前运行时不在受管 Worktree",
                    )
                record = self._record(name)
                status = await self._manager.status(name)
                await ui.show_system_message(self._status_lines(record, status))
                ui.refresh_status(f"已显示 Worktree：{name}")
                return
            if action == "delete" and len(parts) in (2, 3):
                discard = len(parts) == 3 and parts[2] == "--discard"
                if len(parts) == 3 and not discard:
                    raise CommandUsageError
                name = parts[1]
                record = self._record(name)
                if discard:
                    status = await self._manager.status(name)
                    if status.reason_code is not None or not status.exists:
                        raise WorktreeError(
                            "worktree_delete_unsafe",
                            "无法确认 Worktree 删除安全性",
                        )
                    confirmed = await ui.request_confirmation(
                        ConfirmationRequest(
                            "worktree.delete.discard",
                            "永久丢弃 Worktree 修改与提交",
                            (
                                ("name", record.name),
                                ("path", str(record.path)),
                                ("dirty", str(status.dirty).lower()),
                                ("dirty entries", str(status.dirty_entry_count)),
                                (
                                    "unpushed commits",
                                    (
                                        "unknown"
                                        if status.unpushed_commit_count is None
                                        else str(status.unpushed_commit_count)
                                    ),
                                ),
                                (
                                    "has unpushed",
                                    str(status.has_unpushed).lower(),
                                ),
                                ("recovery", "不可恢复"),
                            ),
                            True,
                        )
                    )
                    if not confirmed:
                        ui.refresh_status("已取消 Worktree 删除")
                        return
                status = await self._manager.delete(
                    name,
                    discard_confirmed=discard,
                )
                await ui.show_system_message(
                    (
                        f"已删除 Worktree：{name}",
                        f"dirty entries: {status.dirty_entry_count}",
                        f"had unpushed: {str(status.has_unpushed).lower()}",
                    )
                )
                ui.refresh_status("Worktree 删除完成")
                return
        except CommandUsageError:
            raise
        except WorktreeError as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        raise CommandUsageError

    def _record(self, name: str) -> WorktreeRecord:
        for item in self._manager.list_records():
            if item.name == name:
                return item
        raise WorktreeError("worktree_not_found", "Worktree 不存在")

    @staticmethod
    def _list_line(
        record: WorktreeRecord,
        status: WorktreeStatus,
        *,
        active: bool,
    ) -> str:
        return " | ".join(
            (
                record.name,
                f"kind={record.kind}",
                f"active={str(active).lower()}",
                f"exists={str(status.exists).lower()}",
                f"dirty={status.dirty_entry_count}",
                f"unpushed={str(status.has_unpushed).lower()}",
                f"safe={str(status.deletion_safe).lower()}",
                f"reason={status.reason_code or '-'}",
                f"path={record.path}",
            )
        )

    @staticmethod
    def _status_lines(
        record: WorktreeRecord,
        status: WorktreeStatus,
    ) -> tuple[str, ...]:
        return (
            f"name: {record.name}",
            f"path: {record.path}",
            f"branch: {record.branch}",
            f"kind: {record.kind}",
            f"exists: {str(status.exists).lower()}",
            f"head: {status.head or 'null'}",
            f"dirty: {str(status.dirty).lower()}",
            f"dirty_entry_count: {status.dirty_entry_count}",
            f"upstream: {status.upstream or 'null'}",
            (
                "unpushed_commit_count: "
                + (
                    "null"
                    if status.unpushed_commit_count is None
                    else str(status.unpushed_commit_count)
                )
            ),
            f"has_unpushed: {str(status.has_unpushed).lower()}",
            f"deletion_safe: {str(status.deletion_safe).lower()}",
            f"reason_code: {status.reason_code or 'null'}",
        )
