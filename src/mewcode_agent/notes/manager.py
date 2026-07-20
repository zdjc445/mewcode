"""Five-request scheduling, scoped persistence, Prompt injection, and flush."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from mewcode_agent.agent.usage import NoteUsageRecord, UsageCollector
from mewcode_agent.history import ConversationHistory
from mewcode_agent.notes.models import (
    NoteClearTarget,
    NotePaths,
    NoteScope,
    NoteWarning,
    NotesError,
    NotesSnapshot,
)
from mewcode_agent.notes.storage import load_notes, write_note_scope
from mewcode_agent.notes.updater import NoteGeneration, NoteUpdater
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.prompting.models import RuntimeInstruction
from mewcode_agent.providers.base import ProviderUsageResult

NOTES_TRIGGER_REQUESTS = 5
NOTES_EXIT_TIMEOUT_SECONDS = 120.0


class NotesManager:
    def __init__(
        self,
        updater: NoteUpdater,
        *,
        paths: NotePaths,
        initial_snapshot: NotesSnapshot,
        history: ConversationHistory,
        prompt_runtime: PromptRuntime,
        usage_collector: UsageCollector | None = None,
        warning_handler: Callable[[NoteWarning], None] | None = None,
        exit_timeout_seconds: float = NOTES_EXIT_TIMEOUT_SECONDS,
    ) -> None:
        if exit_timeout_seconds <= 0:
            raise ValueError("exit_timeout_seconds 必须大于 0")
        self._updater = updater
        self._paths = paths
        self._snapshot = initial_snapshot
        self._history = history
        self._prompt_runtime = prompt_runtime
        self._usage_collector = usage_collector
        self._warning_handler = warning_handler
        self._exit_timeout_seconds = exit_timeout_seconds
        self._generation = 1
        self._total_successes = 0
        self._processed_successes = 0
        self._last_attempt_successes = 0
        self._last_success_history_end = 0
        self._pending = False
        self._closing = False
        self._task: asyncio.Task[None] | None = None
        self._operation_lock = asyncio.Lock()

    @property
    def snapshot(self) -> NotesSnapshot:
        return self._snapshot

    @property
    def paths(self) -> NotePaths:
        return self._paths

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def unprocessed_successes(self) -> int:
        return self._total_successes - self._processed_successes

    def _warn(self, scope: NoteScope | None, error: NotesError) -> None:
        if self._warning_handler is not None:
            self._warning_handler(NoteWarning(scope, error.code))

    def record_successful_request(self) -> None:
        if self._closing:
            return
        self._total_successes += 1
        if self._task is not None and not self._task.done():
            if (
                self._total_successes - self._last_attempt_successes
                >= NOTES_TRIGGER_REQUESTS
            ):
                self._pending = True
            return
        if (
            self._total_successes
            - max(self._processed_successes, self._last_attempt_successes)
            >= NOTES_TRIGGER_REQUESTS
        ):
            self._schedule_update()

    def _schedule_update(self) -> None:
        self._last_attempt_successes = self._total_successes
        self._task = asyncio.create_task(self._run_updates())

    async def _run_updates(self) -> None:
        try:
            while True:
                batch_through = self._last_attempt_successes
                async with self._operation_lock:
                    success = await self._attempt_update()
                if success:
                    self._processed_successes = max(
                        self._processed_successes,
                        batch_through,
                    )
                if self._closing:
                    return
                should_continue = self._pending or (
                    self._total_successes
                    - max(
                        self._processed_successes,
                        self._last_attempt_successes,
                    )
                    >= NOTES_TRIGGER_REQUESTS
                )
                self._pending = False
                if not should_continue:
                    return
                self._last_attempt_successes = self._total_successes
        finally:
            self._task = None

    async def _attempt_update(self) -> bool:
        next_generation = self._generation + 1

        def record_usage(result: ProviderUsageResult) -> None:
            if self._usage_collector is not None:
                self._usage_collector.record(
                    NoteUsageRecord(
                        self._updater.provider_id,
                        next_generation,
                        result,
                    )
                )

        try:
            generated = await self._updater.update(
                snapshot=self._snapshot,
                messages=tuple(self._history.snapshot()),
                history_start=self._last_success_history_end,
                on_usage=record_usage,
            )
        except NotesError as exc:
            self._warn(None, exc)
            return False

        scope_results: dict[NoteScope, bool] = {
            "user": False,
            "project": False,
        }
        for scope in ("user", "project"):
            try:
                await asyncio.to_thread(
                    write_note_scope,
                    paths=self._paths,
                    scope=scope,
                    snapshot=generated.snapshot,
                )
            except NotesError as exc:
                self._warn(scope, exc)
            else:
                scope_results[scope] = True

        if not any(scope_results.values()):
            return False
        updated = NotesSnapshot(
            user_preferences=(
                generated.snapshot.user_preferences
                if scope_results["user"]
                else self._snapshot.user_preferences
            ),
            correction_feedback=(
                generated.snapshot.correction_feedback
                if scope_results["user"]
                else self._snapshot.correction_feedback
            ),
            project_knowledge=(
                generated.snapshot.project_knowledge
                if scope_results["project"]
                else self._snapshot.project_knowledge
            ),
            references=(
                generated.snapshot.references
                if scope_results["project"]
                else self._snapshot.references
            ),
        )
        self._generation = next_generation
        self._snapshot = updated
        controls = tuple(
            updated.runtime_control(scope, next_generation)
            for scope in ("project", "user")
            if scope_results[scope]
        )
        try:
            for control in controls:
                self._prompt_runtime.inject(
                    control,
                    history_length=len(self._history.snapshot()),
                )
        except (ValueError, RuntimeError):
            self._warn(None, NotesError("notes_update_failed"))
            return False
        if all(scope_results.values()):
            self._last_success_history_end = generated.history_end
            return True
        return False

    async def clear(self, scope: NoteScope) -> None:
        if scope not in ("user", "project"):
            raise ValueError("scope 必须为 user 或 project")
        async with self._operation_lock:
            updated = NotesSnapshot(
                user_preferences=(
                    ()
                    if scope == "user"
                    else self._snapshot.user_preferences
                ),
                correction_feedback=(
                    ()
                    if scope == "user"
                    else self._snapshot.correction_feedback
                ),
                project_knowledge=(
                    ()
                    if scope == "project"
                    else self._snapshot.project_knowledge
                ),
                references=(
                    () if scope == "project" else self._snapshot.references
                ),
            )
            await asyncio.to_thread(
                write_note_scope,
                paths=self._paths,
                scope=scope,
                snapshot=updated,
            )
            next_generation = self._generation + 1
            control = updated.runtime_control(scope, next_generation)
            self._snapshot = updated
            self._generation = next_generation
            try:
                self._prompt_runtime.inject(
                    control,
                    history_length=len(self._history.snapshot()),
                )
            except (ValueError, RuntimeError) as exc:
                raise NotesError("notes_write_failed") from exc

    def clear_target(self, scope: NoteScope) -> NoteClearTarget:
        if scope == "user":
            return NoteClearTarget(scope, self._paths.user)
        if scope == "project":
            return NoteClearTarget(scope, self._paths.project)
        raise ValueError("scope 必须为 user 或 project")

    async def wait_until_idle(self) -> None:
        task = self._task
        if task is not None:
            await asyncio.shield(task)

    async def flush_before_session_switch(self) -> None:
        await self.wait_until_idle()
        if self.unprocessed_successes <= 0:
            return
        self._last_attempt_successes = self._total_successes
        self._task = asyncio.create_task(self._run_updates())
        task = self._task
        try:
            async with asyncio.timeout(self._exit_timeout_seconds):
                await task
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._warn(None, NotesError("notes_update_failed"))

    def reload_for_session(self) -> tuple[RuntimeInstruction, ...]:
        if self._task is not None and not self._task.done():
            raise NotesError("notes_read_failed")
        snapshot = load_notes(paths=self._paths)
        self._snapshot = snapshot
        self._total_successes = 0
        self._processed_successes = 0
        self._last_attempt_successes = 0
        self._last_success_history_end = 0
        self._pending = False
        return snapshot.runtime_controls(generation=self._generation)

    async def flush_on_exit(self) -> None:
        self._closing = True
        task = self._task
        if task is None and self.unprocessed_successes > 0:
            self._last_attempt_successes = self._total_successes
            self._task = asyncio.create_task(self._run_updates())
            task = self._task
        if task is None:
            return
        try:
            async with asyncio.timeout(self._exit_timeout_seconds):
                await task
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._warn(None, NotesError("notes_update_failed"))
