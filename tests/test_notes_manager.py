from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mewcode_agent.agent.usage import NoteUsageRecord
from mewcode_agent.history import ConversationHistory
from mewcode_agent.notes import (
    NoteGeneration,
    NoteUpdater,
    NoteWarning,
    NotesError,
    NotesManager,
    NotesSnapshot,
    load_notes,
    note_paths,
    write_note_scope,
)
from mewcode_agent.notes import manager as manager_module
from mewcode_agent.prompting.environment import (
    GitEnvironment,
    RequestEnvironment,
    SessionEnvironment,
)
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.providers.base import ProviderUsageResult

USAGE = ProviderUsageResult("unavailable", None, "test")


class FixedCollector:
    async def collect(self) -> RequestEnvironment:
        return RequestEnvironment(
            "2026-07-20T12:00:00+00:00",
            GitEnvironment("not_repository", None, None, None),
        )


class StubUpdater:
    provider_id = "stub"

    def __init__(
        self,
        outcomes: list[NotesSnapshot | NotesError],
        *,
        gate_first: bool = False,
    ) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.gate_first = gate_first

    async def update(
        self,
        *,
        snapshot: NotesSnapshot,
        messages: tuple,
        history_start: int,
        on_usage: Callable[[ProviderUsageResult], None] | None = None,
    ) -> NoteGeneration:
        del snapshot
        self.calls += 1
        call_number = self.calls
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate_first and call_number == 1:
                self.started.set()
                await self.release.wait()
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, NotesError):
                raise outcome
            if on_usage is not None:
                on_usage(USAGE)
            return NoteGeneration(outcome, USAGE, len(messages), 1)
        finally:
            self.active -= 1


class BlockingUpdater:
    provider_id = "blocking"

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = False

    async def update(self, **_kwargs) -> NoteGeneration:
        self.calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@dataclass
class CollectingUsage:
    records: list[object]

    def record(self, record: object) -> None:
        self.records.append(record)


def make_runtime() -> PromptRuntime:
    return PromptRuntime(
        SessionEnvironment(
            "Windows",
            "powershell.exe",
            "D:\\workspace",
            "UTC",
            "+00:00",
        ),
        FixedCollector(),
    )


def make_manager(
    tmp_path: Path,
    updater: object,
    *,
    initial: NotesSnapshot = NotesSnapshot(),
    warning_handler: Callable[[NoteWarning], None] | None = None,
    usage_collector: CollectingUsage | None = None,
    exit_timeout_seconds: float = 1,
) -> tuple[NotesManager, ConversationHistory, PromptRuntime]:
    history = ConversationHistory()
    runtime = make_runtime()
    manager = NotesManager(
        updater,  # type: ignore[arg-type]
        paths=note_paths(user_root=tmp_path / "user", project_root=tmp_path),
        initial_snapshot=initial,
        history=history,
        prompt_runtime=runtime,
        usage_collector=usage_collector,  # type: ignore[arg-type]
        warning_handler=warning_handler,
        exit_timeout_seconds=exit_timeout_seconds,
    )
    return manager, history, runtime


def add_success(
    manager: NotesManager,
    history: ConversationHistory,
    index: int,
) -> None:
    history.add_user(f"request-{index}")
    history.add_assistant(f"response-{index}")
    manager.record_successful_request()


@pytest.mark.asyncio
async def test_five_successes_update_both_scopes_and_inject_generation(
    tmp_path: Path,
) -> None:
    candidate = NotesSnapshot(
        ("preference",),
        ("correction",),
        ("knowledge",),
        ("reference",),
    )
    updater = StubUpdater([candidate])
    usages = CollectingUsage([])
    manager, history, runtime = make_manager(
        tmp_path,
        updater,
        usage_collector=usages,
    )

    for index in range(5):
        add_success(manager, history, index)
    await manager.wait_until_idle()

    assert updater.calls == 1
    assert manager.snapshot == candidate
    assert manager.generation == 2
    assert manager.unprocessed_successes == 0
    assert load_notes(paths=manager.paths) == candidate
    assert [item.instruction_id for item in runtime.timeline()[-2:]] == [
        "runtime.notes.project.generation_2",
        "runtime.notes.user.generation_2",
    ]
    assert [item.anchor for item in runtime.timeline()[-2:]] == [10, 10]
    assert len(usages.records) == 1
    assert isinstance(usages.records[0], NoteUsageRecord)
    assert usages.records[0].request_kind == "notes"


@pytest.mark.asyncio
async def test_concurrent_thresholds_coalesce_with_one_active_update(
    tmp_path: Path,
) -> None:
    first = NotesSnapshot(user_preferences=("first",))
    second = NotesSnapshot(user_preferences=("second",))
    updater = StubUpdater([first, second], gate_first=True)
    manager, history, _runtime = make_manager(tmp_path, updater)

    for index in range(5):
        add_success(manager, history, index)
    await updater.started.wait()
    for index in range(5, 10):
        add_success(manager, history, index)
    updater.release.set()
    await manager.wait_until_idle()

    assert updater.calls == 2
    assert updater.max_active == 1
    assert manager.snapshot.user_preferences == ("second",)
    assert manager.unprocessed_successes == 0


@pytest.mark.asyncio
async def test_failure_retries_only_after_five_new_successes(
    tmp_path: Path,
) -> None:
    warnings: list[NoteWarning] = []
    updater = StubUpdater(
        [
            NotesError("notes_update_failed"),
            NotesSnapshot(user_preferences=("recovered",)),
        ]
    )
    manager, history, _runtime = make_manager(
        tmp_path,
        updater,
        warning_handler=warnings.append,
    )
    for index in range(5):
        add_success(manager, history, index)
    await manager.wait_until_idle()
    assert updater.calls == 1
    assert manager.unprocessed_successes == 5

    for index in range(5, 9):
        add_success(manager, history, index)
    await asyncio.sleep(0)
    assert updater.calls == 1
    add_success(manager, history, 9)
    await manager.wait_until_idle()

    assert updater.calls == 2
    assert manager.unprocessed_successes == 0
    assert warnings == [NoteWarning(None, "notes_update_failed")]


@pytest.mark.asyncio
async def test_scope_write_failure_keeps_old_scope_and_commits_other(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initial = NotesSnapshot(
        user_preferences=("old user",),
        project_knowledge=("old project",),
    )
    candidate = NotesSnapshot(
        user_preferences=("new user",),
        project_knowledge=("new project",),
    )
    paths = note_paths(user_root=tmp_path / "user", project_root=tmp_path)
    write_note_scope(paths=paths, scope="user", snapshot=initial)
    write_note_scope(paths=paths, scope="project", snapshot=initial)
    original_write = manager_module.write_note_scope

    def scoped_write(*, paths, scope, snapshot) -> None:
        if scope == "user":
            raise NotesError("notes_write_failed")
        original_write(paths=paths, scope=scope, snapshot=snapshot)

    monkeypatch.setattr(manager_module, "write_note_scope", scoped_write)
    warnings: list[NoteWarning] = []
    manager, history, runtime = make_manager(
        tmp_path,
        StubUpdater([candidate]),
        initial=initial,
        warning_handler=warnings.append,
    )
    for index in range(5):
        add_success(manager, history, index)
    await manager.wait_until_idle()

    assert manager.snapshot.user_preferences == ("old user",)
    assert manager.snapshot.project_knowledge == ("new project",)
    assert manager.unprocessed_successes == 5
    assert warnings == [NoteWarning("user", "notes_write_failed")]
    assert runtime.timeline()[-1].instruction_id == (
        "runtime.notes.project.generation_2"
    )
    stored = load_notes(paths=paths)
    assert stored.user_preferences == ("old user",)
    assert stored.project_knowledge == ("new project",)


@pytest.mark.asyncio
async def test_exit_flushes_one_unprocessed_success(tmp_path: Path) -> None:
    updater = StubUpdater([NotesSnapshot(user_preferences=("exit",))])
    manager, history, _runtime = make_manager(tmp_path, updater)
    add_success(manager, history, 0)

    await manager.flush_on_exit()

    assert updater.calls == 1
    assert manager.unprocessed_successes == 0


@pytest.mark.asyncio
async def test_session_switch_flushes_pending_without_closing_manager(
    tmp_path: Path,
) -> None:
    updater = StubUpdater(
        [
            NotesSnapshot(user_preferences=("switch",)),
            NotesSnapshot(user_preferences=("later",)),
        ]
    )
    manager, history, _runtime = make_manager(tmp_path, updater)
    add_success(manager, history, 0)

    await manager.flush_before_session_switch()

    assert updater.calls == 1
    assert manager.unprocessed_successes == 0
    manager.reload_for_session()
    for index in range(5):
        add_success(manager, history, index + 1)
    await manager.wait_until_idle()
    assert updater.calls == 2
    assert manager.snapshot.user_preferences == ("later",)


@pytest.mark.asyncio
async def test_exit_without_success_does_not_call_updater(tmp_path: Path) -> None:
    updater = StubUpdater([])
    manager, _history, _runtime = make_manager(tmp_path, updater)

    await manager.flush_on_exit()

    assert updater.calls == 0


@pytest.mark.asyncio
async def test_exit_timeout_cancels_update_and_warns(tmp_path: Path) -> None:
    updater = BlockingUpdater()
    warnings: list[NoteWarning] = []
    manager, history, _runtime = make_manager(
        tmp_path,
        updater,
        warning_handler=warnings.append,
        exit_timeout_seconds=0.01,
    )
    add_success(manager, history, 0)

    await manager.flush_on_exit()

    assert updater.calls == 1
    assert updater.cancelled is True
    assert warnings == [NoteWarning(None, "notes_update_failed")]


@pytest.mark.asyncio
async def test_clear_writes_empty_scope_and_injects_empty_generation(
    tmp_path: Path,
) -> None:
    initial = NotesSnapshot(
        user_preferences=("remove",),
        project_knowledge=("keep",),
    )
    manager, _history, runtime = make_manager(
        tmp_path,
        StubUpdater([]),
        initial=initial,
    )

    await manager.clear("user")

    assert manager.snapshot.user_is_empty()
    assert manager.snapshot.project_knowledge == ("keep",)
    assert manager.generation == 2
    control = runtime.timeline()[-1]
    assert control.instruction_id == "runtime.notes.user.generation_2"
    assert '"user_preferences":[]' in control.content
    assert manager.paths.user.read_text(encoding="utf-8") == (
        "# MewCode User Notes\n\n## 用户偏好\n\n## 纠正反馈\n"
    )


def test_reload_for_session_reads_manual_changes_and_resets_controls(
    tmp_path: Path,
) -> None:
    manager, _history, _runtime = make_manager(
        tmp_path,
        StubUpdater([]),
    )
    changed = NotesSnapshot(references=("manual reference",))
    write_note_scope(
        paths=manager.paths,
        scope="project",
        snapshot=changed,
    )

    controls = manager.reload_for_session()

    assert manager.snapshot == changed
    assert [control.instruction_id for control in controls] == [
        "runtime.notes.project.generation_1"
    ]
