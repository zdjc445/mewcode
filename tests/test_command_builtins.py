from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mewcode_agent.commands import (
    BuiltinCommandServices,
    CommandController,
    CommandMode,
    ConfirmationRequest,
    PermissionCommandPaths,
    build_builtin_command_registry,
)
from mewcode_agent.compaction import ContextStatus, ManualCompactionResult
from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage
from mewcode_agent.notes import NoteClearTarget, NotePaths, NotesSnapshot
from mewcode_agent.security import (
    PathSandbox,
    SecurityBoundary,
    SecurityConfiguration,
    SecurityPolicyEngine,
)
from mewcode_agent.sessions import SessionManager


ACTIVE_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEW_ID = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class FakeAgentLoop:
    def __init__(self) -> None:
        self.compact_calls = 0
        self.prepare_calls = 0
        self.status_calls = 0

    async def compact_history(
        self,
        _history: ConversationHistory,
    ) -> ManualCompactionResult:
        self.compact_calls += 1
        return ManualCompactionResult(True, 3, 4, 900, 500)

    async def prepare_restored_history(
        self,
        _history: ConversationHistory,
    ) -> None:
        self.prepare_calls += 1
        return None

    def context_status(
        self,
        _history: ConversationHistory,
    ) -> ContextStatus:
        self.status_calls += 1
        return ContextStatus(1200, True, 9000, 7200, 2, 8, 1, False)


class FakeNotesManager:
    def __init__(self, tmp_path: Path) -> None:
        self.snapshot = NotesSnapshot(
            user_preferences=("concise",),
            project_knowledge=("entry",),
        )
        self.paths = NotePaths(
            (tmp_path / "user-notes.md").resolve(),
            (tmp_path / "project-notes.md").resolve(),
        )
        self.generation = 4
        self.unprocessed_successes = 2
        self.flush_calls = 0
        self.reload_calls = 0
        self.clear_calls: list[str] = []

    async def flush_before_session_switch(self) -> None:
        self.flush_calls += 1

    def reload_for_session(self) -> tuple[object, ...]:
        self.reload_calls += 1
        self.unprocessed_successes = 0
        return ()

    def clear_target(self, scope: str) -> NoteClearTarget:
        path = self.paths.user if scope == "user" else self.paths.project
        return NoteClearTarget(scope, path)  # type: ignore[arg-type]

    async def clear(self, scope: str) -> None:
        self.clear_calls.append(scope)


@dataclass
class FakeUI:
    messages: list[tuple[str, ...]] = field(default_factory=list)
    confirmations: list[ConfirmationRequest] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    mode: CommandMode = "execute"
    confirmation_result: bool = False
    clear_calls: int = 0

    async def show_system_message(self, lines: tuple[str, ...]) -> None:
        self.messages.append(lines)

    async def request_confirmation(
        self,
        request: ConfirmationRequest,
    ) -> bool:
        self.confirmations.append(request)
        return self.confirmation_result

    async def send_user_message(
        self,
        message: str,
        *,
        mode: CommandMode,
    ) -> None:
        self.sent.append((message, mode))

    def get_default_mode(self) -> CommandMode:
        return self.mode

    def set_default_mode(self, mode: CommandMode) -> None:
        self.mode = mode

    def clear_transcript(self) -> None:
        self.clear_calls += 1

    def refresh_status(self, state: str) -> None:
        self.statuses.append(state)


@dataclass
class BuiltinFixture:
    controller: CommandController
    registry: object
    ui: FakeUI
    loop: FakeAgentLoop
    history: ConversationHistory
    sessions: SessionManager
    notes: FakeNotesManager
    policy: SecurityPolicyEngine
    paths: PermissionCommandPaths
    activations: list[str]
    session_switches: list[tuple[str, bool]]


def make_fixture(
    tmp_path: Path,
    *,
    id_factory=None,
) -> BuiltinFixture:
    history = ConversationHistory()
    sessions = SessionManager(
        sessions_root=tmp_path / "sessions",
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=history,
        id_factory=id_factory or (lambda: ACTIVE_ID),
        now_factory=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    notes = FakeNotesManager(tmp_path)
    policy = SecurityPolicyEngine(
        SecurityConfiguration("strict", (), ()),
        SecurityBoundary(PathSandbox(tmp_path)),
    )
    paths = PermissionCommandPaths(
        (tmp_path / "user-security.yaml").resolve(),
        (tmp_path / "project-security.yaml").resolve(),
        (tmp_path / "security-approvals.yaml").resolve(),
    )
    loop = FakeAgentLoop()
    activations: list[str] = []
    session_switches: list[tuple[str, bool]] = []

    def activate_new() -> None:
        activations.append("new")
        notes.reload_for_session()

    async def session_switched(previous: str, restored: bool) -> None:
        session_switches.append((previous, restored))

    services = BuiltinCommandServices(
        loop,  # type: ignore[arg-type]
        history,
        sessions,
        notes,  # type: ignore[arg-type]
        policy,
        "provider",
        "model",
        paths,
        lambda recovery: activations.append(recovery.meta.session_id),
        activate_new,
        session_switched,
    )
    registry = build_builtin_command_registry(services)
    ui = FakeUI()
    return BuiltinFixture(
        CommandController(registry, ui),
        registry,
        ui,
        loop,
        history,
        sessions,
        notes,
        policy,
        paths,
        activations,
        session_switches,
    )


def test_builtin_catalog_and_status_hints_are_exact(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)

    assert [spec.name for spec in fixture.registry.public_specs()] == [  # type: ignore[union-attr]
        "help",
        "status",
        "mode",
        "compact",
        "clear",
        "sessions",
        "resume",
        "session",
        "memory",
        "permissions",
    ]
    assert fixture.registry.status_hints() == (  # type: ignore[union-attr]
        "/help",
        "/status",
        "/compact",
    )
    assert fixture.registry.resolve("NOTES").name == "memory"  # type: ignore[union-attr]
    assert fixture.registry.resolve("CODE-REVIEW") is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_help_is_generated_from_registry_metadata(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)

    result = await fixture.controller.dispatch("/HELP status")

    assert result.success is True
    assert fixture.ui.messages == [
        (
            "命令：/status",
            "说明：显示模型、会话、Token、笔记和权限状态",
            "类型：local",
            "用法：/status",
            "别名：/stat",
            "参数提示：无",
        )
    ]


@pytest.mark.asyncio
async def test_mode_switches_only_exact_lowercase_argument(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)

    switched = await fixture.controller.dispatch("/MODE plan")
    rejected = await fixture.controller.dispatch("/mode PLAN")

    assert switched.success is True
    assert rejected.success is False
    assert fixture.ui.mode == "plan"
    assert fixture.ui.statuses == ["就绪"]
    assert "command_usage_invalid" in str(fixture.ui.messages[-1])


@pytest.mark.asyncio
async def test_permissions_override_and_reset_never_write_files(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)

    permissive = await fixture.controller.dispatch("/PERMS permissive")
    assert permissive.success is True
    assert fixture.policy.configured_mode == "strict"
    assert fixture.policy.mode == "permissive"
    assert fixture.policy.status().has_runtime_override is True

    reset = await fixture.controller.dispatch("/permissions reset")
    assert reset.success is True
    assert fixture.policy.mode == "strict"
    assert fixture.policy.status().has_runtime_override is False
    for path in (
        fixture.paths.user,
        fixture.paths.project,
        fixture.paths.permanent,
    ):
        assert not path.exists()


@pytest.mark.asyncio
async def test_status_reports_all_local_state_without_mutation(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    fixture.history.add_user("hello")
    history_before = fixture.history.snapshot()

    result = await fixture.controller.dispatch("/STATUS")

    assert result.success is True
    text = "\n".join(fixture.ui.messages[-1])
    for expected in (
        "Provider：provider",
        "Model：model",
        "默认模式：execute",
        f"Session：{ACTIVE_ID}",
        "历史消息：1",
        "estimate=1200",
        "calibrated=是",
        "budget=9000",
        "generation=2",
        "未处理成功请求=2",
        "权限模式：strict",
        "公开命令：10",
    ):
        assert expected in text
    assert fixture.loop.status_calls == 1
    assert fixture.history.snapshot() == history_before


@pytest.mark.asyncio
async def test_clear_preserves_old_session_bytes_and_starts_lazy_new_session(
    tmp_path: Path,
) -> None:
    generated_ids = iter((ACTIVE_ID, NEW_ID))
    fixture = make_fixture(tmp_path, id_factory=lambda: next(generated_ids))
    fixture.history.add_user("preserve this conversation")
    old_directory = tmp_path / "sessions" / ACTIVE_ID
    messages_before = (old_directory / "messages.jsonl").read_bytes()
    meta_before = (old_directory / "meta.json").read_bytes()

    result = await fixture.controller.dispatch("/CLEAR")

    assert result.success is True
    assert fixture.sessions.active_session_id == NEW_ID
    assert fixture.history.snapshot() == []
    assert not (tmp_path / "sessions" / NEW_ID).exists()
    assert (old_directory / "messages.jsonl").read_bytes() == messages_before
    assert (old_directory / "meta.json").read_bytes() == meta_before
    assert fixture.notes.flush_calls == 1
    assert fixture.activations == ["new"]
    assert fixture.session_switches == [(ACTIVE_ID, False)]
    assert fixture.ui.clear_calls == 1


@pytest.mark.asyncio
async def test_failed_clear_does_not_emit_session_switch(tmp_path: Path) -> None:
    fixture = make_fixture(
        tmp_path,
        id_factory=lambda: ACTIVE_ID,
    )

    result = await fixture.controller.dispatch("/clear")

    assert result.success is False
    assert fixture.sessions.active_session_id == ACTIVE_ID
    assert fixture.session_switches == []


@pytest.mark.asyncio
async def test_memory_clear_uses_generic_confirmation(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    fixture.ui.confirmation_result = True

    result = await fixture.controller.dispatch("/NOTES clear project")

    assert result.success is True
    assert fixture.notes.clear_calls == ["project"]
    request = fixture.ui.confirmations[0]
    assert request.action_id == "notes.clear"
    assert request.fields == (
        ("scope", "project"),
        ("路径", str(fixture.notes.paths.project)),
    )
