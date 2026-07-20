"""Slash-command views and cancellation for worker tasks."""

from __future__ import annotations

from mewcode_agent.commands.models import (
    CommandDomainError,
    CommandInvocation,
    CommandSpec,
    CommandUI,
    CommandUsageError,
    ConfirmationRequest,
)
from mewcode_agent.workers.catalog import WorkerCatalog
from mewcode_agent.workers.manager import WorkerManager
from mewcode_agent.workers.models import WorkerError, WorkerTaskSnapshot


class WorkerCommandManager:
    def __init__(
        self,
        catalog: WorkerCatalog,
        manager: WorkerManager,
    ) -> None:
        if not isinstance(catalog, WorkerCatalog):
            raise ValueError("catalog 类型无效")
        if not isinstance(manager, WorkerManager):
            raise ValueError("manager 类型无效")
        self._catalog = catalog
        self._manager = manager

    def specs(self) -> tuple[CommandSpec, CommandSpec]:
        return (
            CommandSpec(
                "workers",
                (),
                "列出 Worker 任务或生效角色",
                "/workers [roles]",
                "local",
                "workflow",
                "省略参数或精确小写 roles",
                self._workers,
            ),
            CommandSpec(
                "worker",
                (),
                "显示或终止一个 Worker 任务",
                "/worker <show|cancel> <task_id>",
                "local",
                "workflow",
                "精确小写子命令与 32 位小写十六进制 task ID",
                self._worker,
            ),
        )

    async def _workers(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        if invocation.arguments == "roles":
            lines = tuple(
                " | ".join(
                    (
                        definition.name,
                        definition.description,
                        f"source={definition.source}",
                        f"model={definition.model}",
                        f"max_rounds={definition.max_rounds}",
                        f"isolation={definition.isolation}",
                    )
                )
                for definition in self._catalog.snapshot.definitions
            ) or ("当前没有生效 Worker 角色",)
            await ui.show_system_message(lines)
            ui.refresh_status("已显示 Worker 角色")
            return
        if invocation.arguments:
            raise CommandUsageError
        snapshots = await self._manager.list()
        lines = tuple(self._list_line(snapshot) for snapshot in snapshots)
        await ui.show_system_message(lines or ("当前没有 Worker 任务",))
        ui.refresh_status(f"已显示 {len(snapshots)} 个 Worker 任务")

    async def _worker(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        parts = invocation.arguments.split(" ")
        if len(parts) != 2 or parts[0] not in ("show", "cancel"):
            raise CommandUsageError
        action, task_id = parts
        if not _valid_task_id(task_id):
            raise CommandUsageError
        try:
            if action == "cancel":
                snapshot = await self._manager.get(task_id)
                if snapshot.state not in ("starting", "running"):
                    raise CommandDomainError(
                        "worker_task_terminal",
                        "Worker task 已结束",
                    )
                confirmed = await ui.request_confirmation(
                    ConfirmationRequest(
                        "worker.cancel",
                        "终止 Worker 任务",
                        (
                            ("task ID", snapshot.task_id),
                            ("type", snapshot.worker_type),
                            ("task", snapshot.task),
                        ),
                        True,
                    )
                )
                if not confirmed:
                    ui.refresh_status("已取消终止 Worker")
                    return
                changed = await self._manager.cancel(task_id)
                if not changed:
                    raise CommandDomainError(
                        "worker_task_terminal",
                        "Worker task 已结束",
                    )
                await ui.show_system_message(
                    (f"Worker {task_id} 已取消",)
                )
                ui.refresh_status("Worker 取消操作完成")
                return
            snapshot = await self._manager.get(task_id)
        except WorkerError as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        await ui.show_system_message(self._detail_lines(snapshot))
        ui.refresh_status(f"已显示 Worker：{task_id}")

    @staticmethod
    def _list_line(snapshot: WorkerTaskSnapshot) -> str:
        usage = snapshot.usage
        return " | ".join(
            (
                snapshot.task_id,
                snapshot.worker_type,
                snapshot.state,
                snapshot.mode,
                (
                    "tokens="
                    f"{usage.prompt_tokens + usage.completion_tokens}"
                ),
                f"started={snapshot.started_at or '-'}",
                f"ended={snapshot.ended_at or '-'}",
            )
        )

    @staticmethod
    def _detail_lines(snapshot: WorkerTaskSnapshot) -> tuple[str, ...]:
        usage = snapshot.usage
        return (
            f"task_id: {snapshot.task_id}",
            f"session_id: {snapshot.session_id}",
            f"type: {snapshot.worker_type}",
            f"kind: {snapshot.kind}",
            f"state: {snapshot.state}",
            f"mode: {snapshot.mode}",
            f"transition: {snapshot.transition or 'null'}",
            f"provider_id: {snapshot.provider_id}",
            f"model: {snapshot.model}",
            "visible_tools: " + ", ".join(snapshot.visible_tools),
            f"created_at: {snapshot.created_at}",
            f"started_at: {snapshot.started_at or 'null'}",
            f"ended_at: {snapshot.ended_at or 'null'}",
            (
                "usage: "
                f"prompt={usage.prompt_tokens}, "
                f"hit={usage.cache_hit_tokens}, "
                f"miss={usage.cache_miss_tokens}, "
                f"completion={usage.completion_tokens}, "
                f"unavailable_rounds={usage.unavailable_rounds}"
            ),
            f"error_code: {snapshot.error_code or 'null'}",
            (
                "workspace: null"
                if snapshot.workspace is None
                else (
                    "workspace: "
                    f"path={snapshot.workspace.path}, "
                    f"preserved={snapshot.workspace.preserved}, "
                    f"reason={snapshot.workspace.reason or 'null'}"
                )
            ),
            (
                "report_format_valid: "
                + (
                    "null"
                    if snapshot.report_format_valid is None
                    else str(snapshot.report_format_valid).lower()
                )
            ),
            "result:",
            snapshot.result or "",
        )


def _valid_task_id(value: str) -> bool:
    return len(value) == 32 and all(
        character in "0123456789abcdef" for character in value
    )
