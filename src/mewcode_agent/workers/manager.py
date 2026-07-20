"""Concurrent foreground/background worker task ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from mewcode_agent.workers.models import (
    WorkerCloseResult,
    WorkerError,
    WorkerExecutionOutcome,
    WorkerExecutionSpec,
    WorkerMode,
    WorkerNotification,
    WorkerRuntimeConfig,
    WorkerState,
    WorkerTaskSnapshot,
    WorkerTransition,
)
from mewcode_agent.workers.usage import WorkerUsageCollector


WorkerRunner = Callable[
    [WorkerExecutionSpec, WorkerUsageCollector],
    Awaitable[WorkerExecutionOutcome],
]
WorkerCancelRunner = Callable[[str], bool]


@dataclass(slots=True)
class _WorkerRecord:
    spec: WorkerExecutionSpec
    state: WorkerState
    mode: WorkerMode
    transition: WorkerTransition | None
    created_at: str
    started_at: str | None
    ended_at: str | None
    usage: WorkerUsageCollector
    result: str | None = None
    error_code: str | None = None
    report_format_valid: bool | None = None
    notified: bool = False


class WorkerManager:
    def __init__(
        self,
        runtime_config: WorkerRuntimeConfig,
        runner: WorkerRunner,
        *,
        now: Callable[[], datetime] | None = None,
        cancel_runner: WorkerCancelRunner | None = None,
    ) -> None:
        if not callable(runner):
            raise ValueError("runner 必须可调用")
        self._config = runtime_config
        self._runner = runner
        self._now = now or (lambda: datetime.now().astimezone())
        self._cancel_runner = cancel_runner
        self._records: dict[str, _WorkerRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._notifications: dict[str, list[str]] = {}
        self._foreground_task_id: str | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._close_result: WorkerCloseResult | None = None

    def _timestamp(self) -> str:
        current = self._now()
        if current.utcoffset() is None:
            raise ValueError("Worker 时间必须包含 UTC offset")
        return current.isoformat()

    async def start(
        self,
        spec: WorkerExecutionSpec,
        *,
        background: bool,
        transition: WorkerTransition | None,
    ) -> WorkerTaskSnapshot:
        async with self._lock:
            if self._closed:
                raise WorkerError("worker_manager_closed", "Worker Manager 已关闭")
            active = sum(
                record.state in ("starting", "running")
                for record in self._records.values()
            )
            if active >= self._config.max_concurrency:
                raise WorkerError(
                    "worker_capacity_reached",
                    "Worker 并发容量已满",
                )
            if not background and self._foreground_task_id is not None:
                raise WorkerError(
                    "worker_capacity_reached",
                    "已有前台 Worker 正在等待",
                )
            if spec.task_id in self._records:
                raise ValueError("task_id 已存在")
            mode: WorkerMode = "background" if background else "foreground"
            if background and transition is None:
                raise ValueError("后台 Worker 必须声明 transition")
            if not background and transition is not None:
                raise ValueError("前台 Worker 不能预设 transition")
            record = _WorkerRecord(
                spec,
                "starting",
                mode,
                transition,
                self._timestamp(),
                None,
                None,
                WorkerUsageCollector(),
            )
            self._records[spec.task_id] = record
            if not background:
                self._foreground_task_id = spec.task_id
            task = asyncio.create_task(
                self._drive(record),
                name=f"mewcode-worker-{spec.task_id}",
            )
            self._tasks[spec.task_id] = task
            return self._snapshot(record)

    async def _drive(self, record: _WorkerRecord) -> None:
        async with self._lock:
            record.state = "running"
            record.started_at = self._timestamp()
        try:
            outcome = await self._runner(record.spec, record.usage)
        except asyncio.CancelledError:
            await self._finish(record, "cancelled", error_code="worker_cancelled")
            raise
        except WorkerError as exc:
            await self._finish(
                record,
                "cancelled" if exc.code == "worker_cancelled" else "failed",
                error_code=exc.code,
            )
        except Exception:
            await self._finish(record, "failed", error_code="worker_failed")
        else:
            await self._finish(
                record,
                "completed",
                result=outcome.result,
                report_format_valid=outcome.report_format_valid,
            )

    async def _finish(
        self,
        record: _WorkerRecord,
        state: Literal["completed", "failed", "cancelled"],
        *,
        result: str | None = None,
        error_code: str | None = None,
        report_format_valid: bool | None = None,
    ) -> None:
        async with self._lock:
            if record.state in ("completed", "failed", "cancelled"):
                return
            record.state = state
            record.ended_at = self._timestamp()
            record.result = result
            record.error_code = error_code
            record.report_format_valid = report_format_valid
            if self._foreground_task_id == record.spec.task_id:
                self._foreground_task_id = None
            if record.mode == "background" and not record.notified:
                self._notifications.setdefault(record.spec.session_id, []).append(
                    record.spec.task_id
                )
                record.notified = True

    async def wait_foreground(self, task_id: str) -> WorkerTaskSnapshot:
        async with self._lock:
            record = self._require_record(task_id)
            if record.mode != "foreground":
                return self._snapshot(record)
            task = self._tasks[task_id]
        try:
            async with asyncio.timeout(self._config.foreground_timeout_seconds):
                await asyncio.shield(task)
        except TimeoutError:
            await self.detach(task_id, transition="timeout")
        except asyncio.CancelledError:
            await self.detach(task_id, transition="timeout")
            raise
        return await self.get(task_id)

    async def detach(
        self,
        task_id: str,
        *,
        transition: WorkerTransition,
    ) -> bool:
        if transition not in ("timeout", "escape"):
            raise ValueError("detach transition 必须是 timeout 或 escape")
        async with self._lock:
            record = self._require_record(task_id)
            if record.state not in ("starting", "running"):
                return False
            if record.mode == "background":
                return False
            record.mode = "background"
            record.transition = transition
            if self._foreground_task_id == task_id:
                self._foreground_task_id = None
            return True

    async def detach_foreground(self) -> str | None:
        async with self._lock:
            task_id = self._foreground_task_id
        if task_id is None:
            return None
        detached = await self.detach(task_id, transition="escape")
        return task_id if detached else None

    async def get(self, task_id: str) -> WorkerTaskSnapshot:
        async with self._lock:
            return self._snapshot(self._require_record(task_id))

    async def list(self) -> tuple[WorkerTaskSnapshot, ...]:
        async with self._lock:
            return tuple(self._snapshot(record) for record in self._records.values())

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            record = self._require_record(task_id)
            if record.state not in ("starting", "running"):
                return False
            task = self._tasks[task_id]
            if self._cancel_runner is not None:
                self._cancel_runner(task_id)
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self._finish(record, "cancelled", error_code="worker_cancelled")
        return True

    async def take_notifications(
        self,
        session_id: str,
    ) -> tuple[WorkerNotification, ...]:
        async with self._lock:
            task_ids = tuple(self._notifications.pop(session_id, ()))
            return tuple(
                self._notification(self._records[task_id])
                for task_id in task_ids
            )

    async def clear_notifications(self, session_id: str) -> int:
        async with self._lock:
            return len(self._notifications.pop(session_id, ()))

    async def close(self) -> WorkerCloseResult:
        async with self._lock:
            if self._close_result is not None:
                return self._close_result
            self._closed = True
            active = tuple(
                (self._records[task_id], task)
                for task_id, task in self._tasks.items()
                if self._records[task_id].state in ("starting", "running")
            )
            for record, _ in active:
                if self._cancel_runner is not None:
                    self._cancel_runner(record.spec.task_id)
        if active:
            await asyncio.sleep(0)
            cancelled = 0
            for _, task in active:
                if not task.done():
                    task.cancel()
                    cancelled += 1
            await asyncio.gather(
                *(task for _, task in active),
                return_exceptions=True,
            )
            for record, _ in active:
                await self._finish(
                    record,
                    "cancelled",
                    error_code="worker_cancelled",
                )
        else:
            cancelled = 0
        async with self._lock:
            cleared_notifications = sum(
                len(items) for items in self._notifications.values()
            )
            self._notifications.clear()
            self._close_result = WorkerCloseResult(
                len(active),
                cancelled,
                cleared_notifications,
            )
            return self._close_result

    def _require_record(self, task_id: str) -> _WorkerRecord:
        record = self._records.get(task_id)
        if record is None:
            raise WorkerError("worker_task_not_found", "Worker task 不存在")
        return record

    @staticmethod
    def _snapshot(record: _WorkerRecord) -> WorkerTaskSnapshot:
        spec = record.spec
        return WorkerTaskSnapshot(
            spec.task_id,
            spec.session_id,
            spec.worker_type,
            spec.kind,
            record.state,
            record.mode,
            record.transition,
            spec.task,
            spec.provider_id,
            spec.model,
            tuple(sorted(spec.visible_tools)),
            record.created_at,
            record.started_at,
            record.ended_at,
            record.usage.snapshot(),
            record.result,
            record.error_code,
            record.report_format_valid,
        )

    @staticmethod
    def _notification(record: _WorkerRecord) -> WorkerNotification:
        assert record.state in ("completed", "failed", "cancelled")
        result = record.result or ""
        if len(result) > 8000:
            marker = "\n...[worker result truncated]...\n"
            result = result[:5900] + marker + result[-2000:]
        return WorkerNotification(
            record.spec.task_id,
            record.spec.worker_type,
            record.state,
            record.usage.snapshot(),
            result,
            record.error_code,
        )
