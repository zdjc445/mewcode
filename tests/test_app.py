from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
import inspect
from pathlib import Path

import pytest
from textual.widgets import Button, Input, RichLog, Static, Switch

from mewcode_agent.agent import (
    AgentEvent,
    AgentRunContext,
    FinalResponseEvent,
    ModelTextEvent,
    ModelThinkingEvent,
    PlanApprovalRequestedEvent,
    PlanApprovalResolution,
    RoundStartedEvent,
    RunCancelledEvent,
    RunErrorEvent,
    ToolApprovalRequestedEvent,
    ToolCallStartedEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from mewcode_agent.agent.context import AgentRunCancelled
import mewcode_agent.app as app_module
from mewcode_agent.app import ChatApp
from mewcode_agent.commands import (
    BuiltinCommandServices,
    PermissionCommandPaths,
    build_builtin_command_registry,
)
from mewcode_agent.compaction import ManualCompactionResult
from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage, ToolCall
from mewcode_agent.notes import NoteClearTarget, NotePaths, NotesSnapshot
from mewcode_agent.security import (
    PathSandbox,
    SecurityBoundary,
    SecurityConfiguration,
    SecurityPolicyEngine,
)
from mewcode_agent.sessions import SessionJournal, SessionManager
from mewcode_agent.tools.base import ToolResult


class GatedAgentLoop:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.plan_only_values: list[bool] = []

    async def run(
        self,
        user_message: str,
        history: ConversationHistory,
        *,
        plan_only: bool,
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        context.begin_run()
        self.plan_only_values.append(plan_only)
        history.add_user(user_message)
        try:
            yield UserMessageEvent(user_message)
            yield RoundStartedEvent(
                1,
                15,
                "planning" if plan_only else "executing",
            )
            yield ModelThinkingEvent("分析")
            yield ModelTextEvent("分片")
            self.started.set()
            await self.release.wait()
            history.add_assistant("分片完成")
            yield ModelTextEvent("完成")
            yield FinalResponseEvent("分片完成", 1)
        finally:
            context.finish_run()


class ErrorAgentLoop:
    async def run(
        self,
        user_message: str,
        history: ConversationHistory,
        *,
        plan_only: bool,
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        context.begin_run()
        history.add_user(user_message)
        try:
            yield UserMessageEvent(user_message)
            yield RunErrorEvent("provider_error", "模拟失败")
        finally:
            context.finish_run()


class ToolApprovalAgentLoop:
    def __init__(self) -> None:
        self.decision: str | None = None
        self.cancelled = False

    async def run(
        self,
        user_message: str,
        history: ConversationHistory,
        *,
        plan_only: bool,
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        context.begin_run()
        history.add_user(user_message)
        try:
            yield UserMessageEvent(user_message)
            request_id = context.open_tool_approval()
            yield ToolApprovalRequestedEvent(
                request_id,
                "call-1",
                "write_file",
                '{"path":"README.md"}',
                "write",
            )
            try:
                self.decision = await context.wait_for_tool_approval(
                    request_id
                )
                yield RunCancelledEvent("test_complete")
            except AgentRunCancelled:
                self.cancelled = True
                yield RunCancelledEvent("user_cancelled")
        finally:
            context.finish_run()


class PlanApprovalAgentLoop:
    def __init__(
        self,
        *,
        can_execute: bool = True,
        can_request_changes: bool = True,
    ) -> None:
        self.can_execute = can_execute
        self.can_request_changes = can_request_changes
        self.resolution: PlanApprovalResolution | None = None
        self.cancelled = False

    async def run(
        self,
        user_message: str,
        history: ConversationHistory,
        *,
        plan_only: bool,
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        context.begin_run()
        history.add_user(user_message)
        history.add_assistant("实施计划")
        try:
            yield UserMessageEvent(user_message)
            request_id = context.open_plan_approval()
            yield PlanApprovalRequestedEvent(
                request_id,
                "实施计划",
                self.can_execute,
                self.can_request_changes,
            )
            try:
                self.resolution = await context.wait_for_plan_approval(
                    request_id
                )
                yield RunCancelledEvent("test_complete")
            except AgentRunCancelled:
                self.cancelled = True
                yield RunCancelledEvent("user_cancelled")
        finally:
            context.finish_run()


class GatedToolEventAgentLoop:
    def __init__(self) -> None:
        self.tool_started = asyncio.Event()
        self.release_tool = asyncio.Event()
        self.result_emitted = asyncio.Event()
        self.release_result = asyncio.Event()

    async def run(
        self,
        user_message: str,
        history: ConversationHistory,
        *,
        plan_only: bool,
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        context.begin_run()
        history.add_user(user_message)
        call = ToolCall("call-1", "read_file", '{"path":"README.md"}')
        result = ToolResult("read_file", True, data={"content": "说明"})
        try:
            yield UserMessageEvent(user_message)
            yield RoundStartedEvent(1, 15, "executing")
            history.add_assistant_tool_calls("", (call,))
            yield ToolCallStartedEvent(
                call.call_id,
                call.name,
                call.arguments_json,
                "read",
            )
            self.tool_started.set()
            await self.release_tool.wait()
            history.add_tool_result(call.call_id, result)
            yield ToolResultEvent(call.call_id, result)
            self.result_emitted.set()
            await self.release_result.wait()
            history.add_assistant("读取完成")
            yield FinalResponseEvent("读取完成", 2)
        finally:
            context.finish_run()


class ManualCompactionAgentLoop:
    def __init__(self) -> None:
        self.compact_calls = 0

    async def compact_history(
        self,
        history: ConversationHistory,
    ) -> ManualCompactionResult:
        self.compact_calls += 1
        assert len(history.snapshot()) == 1
        return ManualCompactionResult(True, 2, 1, 900, 500)


class GatedManualCompactionAgentLoop:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def compact_history(
        self,
        history: ConversationHistory,
    ) -> ManualCompactionResult:
        del history
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class RestorableAgentLoop:
    def __init__(self) -> None:
        self.prepare_calls = 0

    async def prepare_restored_history(
        self,
        history: ConversationHistory,
    ) -> None:
        self.prepare_calls += 1
        assert len(history.snapshot()) >= 1
        return None


class FakeNotesManager:
    def __init__(self, tmp_path: Path) -> None:
        self.snapshot = NotesSnapshot(
            user_preferences=("concise",),
            correction_feedback=("keep exact names",),
            project_knowledge=("entry src/main.py",),
            references=("docs/spec.md",),
        )
        self.paths = NotePaths(
            (tmp_path / "user-notes.md").resolve(),
            (tmp_path / "project-notes.md").resolve(),
        )
        self.successes = 0
        self.clear_calls: list[str] = []
        self.generation = 1
        self.unprocessed_successes = 0

    def record_successful_request(self) -> None:
        self.successes += 1

    async def wait_until_idle(self) -> None:
        return None

    async def flush_before_session_switch(self) -> None:
        return None

    def reload_for_session(self) -> tuple[object, ...]:
        self.unprocessed_successes = 0
        return ()

    def clear_target(self, scope: str) -> NoteClearTarget:
        path = self.paths.user if scope == "user" else self.paths.project
        return NoteClearTarget(scope, path)  # type: ignore[arg-type]

    async def clear(self, scope: str) -> None:
        self.clear_calls.append(scope)


def make_app(
    loop: object,
    history: ConversationHistory | None = None,
) -> ChatApp:
    return ChatApp(
        loop,  # type: ignore[arg-type]
        history if history is not None else ConversationHistory(),
        provider_id="deepseek_openai",
        model="deepseek-v4-pro",
    )


def render_log_text(log: RichLog) -> str:
    return "\n".join(strip.text for strip in log.lines)


def make_session_app(
    tmp_path: Path,
    loop: object,
    *,
    active_id: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
) -> tuple[ChatApp, SessionManager, ConversationHistory, list[str]]:
    history = ConversationHistory()
    manager = SessionManager(
        sessions_root=tmp_path / "sessions",
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=history,
        id_factory=lambda: active_id,
        now_factory=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    activations: list[str] = []
    notes = FakeNotesManager(tmp_path)
    policy = SecurityPolicyEngine(
        SecurityConfiguration("default", (), ()),
        SecurityBoundary(PathSandbox(tmp_path)),
    )
    registry = build_builtin_command_registry(
        BuiltinCommandServices(
            loop,  # type: ignore[arg-type]
            history,
            manager,
            notes,  # type: ignore[arg-type]
            policy,
            "provider",
            "model",
            PermissionCommandPaths(
                (tmp_path / "user-security.yaml").resolve(),
                (tmp_path / "project-security.yaml").resolve(),
                (tmp_path / "approvals.yaml").resolve(),
            ),
            lambda recovery: activations.append(recovery.meta.session_id),
            lambda: None,
        )
    )
    app = ChatApp(
        loop,  # type: ignore[arg-type]
        history,
        provider_id="provider",
        model="model",
        command_registry=registry,
        notes_manager=notes,  # type: ignore[arg-type]
    )
    return app, manager, history, activations


def make_builtin_app(
    tmp_path: Path,
    loop: object,
    history: ConversationHistory,
    *,
    notes: FakeNotesManager | None = None,
) -> ChatApp:
    selected_notes = notes or FakeNotesManager(tmp_path)
    manager = SessionManager(
        sessions_root=tmp_path / "sessions",
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=history,
    )
    policy = SecurityPolicyEngine(
        SecurityConfiguration("default", (), ()),
        SecurityBoundary(PathSandbox(tmp_path)),
    )
    registry = build_builtin_command_registry(
        BuiltinCommandServices(
            loop,  # type: ignore[arg-type]
            history,
            manager,
            selected_notes,  # type: ignore[arg-type]
            policy,
            "provider",
            "model",
            PermissionCommandPaths(
                (tmp_path / "user-security.yaml").resolve(),
                (tmp_path / "project-security.yaml").resolve(),
                (tmp_path / "approvals.yaml").resolve(),
            ),
            lambda _recovery: None,
            lambda: None,
        )
    )
    return ChatApp(
        loop,  # type: ignore[arg-type]
        history,
        provider_id="provider",
        model="model",
        command_registry=registry,
        notes_manager=selected_notes,  # type: ignore[arg-type]
    )


def test_app_has_no_prompt_assembly_or_provider_usage_dependency() -> None:
    source = inspect.getsource(app_module)

    for forbidden in (
        "ProviderUsageEvent",
        "ProviderUsageResult",
        "PromptRuntime",
        "PromptComposer",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "parse_note_command",
        "parse_session_command",
        'prompt == "/compact"',
    ):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_app_consumes_agent_events_and_restores_input() -> None:
    loop = GatedAgentLoop()
    history = ConversationHistory()
    app = make_app(loop, history)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        plan_switch = app.query_one("#plan-only-switch", Switch)
        assert plan_switch.value is False
        plan_switch.value = True
        prompt_input.value = "记住 42"
        await pilot.press("enter")
        await loop.started.wait()
        await pilot.pause()

        assert prompt_input.disabled is True
        assert plan_switch.disabled is True
        assert app.active_thinking == "分析"
        assert app.active_response == "分片"
        log_text = render_log_text(app.query_one("#chat-log", RichLog))
        assert "Thinking: 分析" in log_text
        assert "Assistant: 分片" in log_text

        loop.release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert prompt_input.disabled is False
        assert prompt_input.has_focus is True
        assert plan_switch.disabled is False
        assert plan_switch.value is True
        assert loop.plan_only_values == [True]
        assert history.snapshot() == [
            ChatMessage(role="user", content="记住 42"),
            ChatMessage(role="assistant", content="分片完成"),
        ]


@pytest.mark.asyncio
async def test_app_ignores_blank_input() -> None:
    loop = GatedAgentLoop()
    history = ConversationHistory()
    app = make_app(loop, history)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "   "
        await pilot.press("enter")
        await pilot.pause()

        assert len(history) == 0
        assert prompt_input.disabled is False
        assert not loop.started.is_set()


@pytest.mark.asyncio
async def test_status_bar_keeps_mode_and_registry_hints(tmp_path: Path) -> None:
    app = make_builtin_app(
        tmp_path,
        RestorableAgentLoop(),
        ConversationHistory(),
    )

    async with app.run_test() as pilot:
        status = app.query_one("#status", Static)
        initial = str(status.render())
        assert "mode=execute" in initial
        assert "/help /status /compact" in initial

        app.query_one("#plan-only-switch", Switch).value = True
        await pilot.pause()

        changed = str(status.render())
        assert "mode=plan" in changed
        assert "/help /status /compact" in changed


@pytest.mark.asyncio
async def test_tab_single_match_completes_and_multiple_matches_open_popup(
    tmp_path: Path,
) -> None:
    app = make_builtin_app(
        tmp_path,
        RestorableAgentLoop(),
        ConversationHistory(),
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "/clea"
        prompt_input.cursor_position = len(prompt_input.value)
        await pilot.press("tab")
        await pilot.pause()
        assert prompt_input.value == "/clear "

        prompt_input.value = "/s"
        prompt_input.cursor_position = len(prompt_input.value)
        await pilot.press("tab")
        await pilot.pause()
        assert isinstance(app.screen, app_module.CommandCompletionScreen)
        await pilot.press("down", "enter")
        await pilot.pause()
        assert prompt_input.value in {
            "/status ",
            "/stat ",
            "/sessions ",
            "/session ",
        }


@pytest.mark.asyncio
async def test_case_insensitive_compact_command_does_not_enter_history(
    tmp_path: Path,
) -> None:
    loop = ManualCompactionAgentLoop()
    history = ConversationHistory()
    history.add_assistant("旧回复")
    app = make_builtin_app(tmp_path, loop, history)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "  /COMPACT  "
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        status = str(app.query_one("#status", Static).render())
        assert "generation=2" in status
        assert "覆盖消息=1" in status
        assert "估算减少=400" in status
        assert prompt_input.disabled is False

    assert loop.compact_calls == 1
    assert history.snapshot() == [
        ChatMessage(role="assistant", content="旧回复")
    ]


@pytest.mark.asyncio
async def test_invalid_compact_arguments_are_consumed_without_agent_run(
    tmp_path: Path,
) -> None:
    loop = GatedAgentLoop()
    history = ConversationHistory()
    app = make_builtin_app(tmp_path, loop, history)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "/compact extra"
        await pilot.press("enter")
        await app.workers.wait_for_complete()

    assert history.snapshot() == []
    assert loop.started.is_set() is False


@pytest.mark.asyncio
async def test_sessions_command_lists_meta_without_entering_history(
    tmp_path: Path,
) -> None:
    target_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    target = SessionJournal(
        sessions_root=tmp_path / "sessions",
        session_id=target_id,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=lambda: datetime(2026, 7, 19, tzinfo=timezone.utc),
    )
    target.append(ChatMessage(role="user", content="saved title"))
    target.close()
    loop = RestorableAgentLoop()
    app, _manager, history, _activations = make_session_app(tmp_path, loop)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "/sessions"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        log_text = render_log_text(app.query_one("#chat-log", RichLog))
        assert target_id in log_text
        assert "saved title" in log_text.replace("\n", "")
        assert "已列出 1 个会话" in str(
            app.query_one("#status", Static).render()
        )

    assert history.snapshot() == []
    assert loop.prepare_calls == 0


@pytest.mark.asyncio
async def test_session_path_command_outputs_exact_absolute_path(
    tmp_path: Path,
) -> None:
    target_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    target = SessionJournal(
        sessions_root=tmp_path / "sessions",
        session_id=target_id,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
    )
    target.append(ChatMessage(role="user", content="saved"))
    target.close()
    app, manager, history, _activations = make_session_app(
        tmp_path,
        RestorableAgentLoop(),
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = f"/session path {target_id}"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert str(manager.session_path(target_id)) in render_log_text(
            app.query_one("#chat-log", RichLog)
        ).replace("\n", "")

    assert history.snapshot() == []


@pytest.mark.asyncio
async def test_resume_command_switches_history_and_runs_restoration(
    tmp_path: Path,
) -> None:
    target_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    target = SessionJournal(
        sessions_root=tmp_path / "sessions",
        session_id=target_id,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
    )
    target.append(ChatMessage(role="user", content="restored message"))
    target.close()
    loop = RestorableAgentLoop()
    app, manager, history, activations = make_session_app(tmp_path, loop)
    history.add_user("current message")

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = f"/resume {target_id}"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert "会话已恢复" in str(
            app.query_one("#status", Static).render()
        )
        assert "restored message" in render_log_text(
            app.query_one("#chat-log", RichLog)
        )

    assert manager.active_session_id == target_id
    assert history.snapshot() == [
        ChatMessage(role="user", content="restored message")
    ]
    assert activations == [target_id]
    assert loop.prepare_calls == 1


@pytest.mark.asyncio
async def test_delete_command_requires_modal_confirmation(
    tmp_path: Path,
) -> None:
    target_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    target = SessionJournal(
        sessions_root=tmp_path / "sessions",
        session_id=target_id,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
    )
    target.append(ChatMessage(role="user", content="delete title"))
    target.close()
    target_path = tmp_path / "sessions" / target_id
    app, _manager, history, _activations = make_session_app(
        tmp_path,
        RestorableAgentLoop(),
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = f"/session delete {target_id}"
        await pilot.press("enter")
        await pilot.pause()

        screen_text = "\n".join(
            str(widget.render()) for widget in app.screen.query(Static)
        )
        assert target_id in screen_text
        assert "delete title" in screen_text
        assert str(target_path.resolve()) in screen_text
        assert target_path.exists()

        await pilot.click("#confirm-command")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert not target_path.exists()
        assert "会话已删除" in str(
            app.query_one("#status", Static).render()
        )

    assert history.snapshot() == []


@pytest.mark.asyncio
async def test_unknown_slash_command_is_consumed_without_agent_run(
    tmp_path: Path,
) -> None:
    loop = GatedAgentLoop()
    app, _manager, history, _activations = make_session_app(tmp_path, loop)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "/Missing"
        await pilot.press("enter")
        await app.workers.wait_for_complete()

    assert history.snapshot() == []
    assert loop.started.is_set() is False


@pytest.mark.asyncio
async def test_notes_show_and_paths_commands_do_not_enter_history(
    tmp_path: Path,
) -> None:
    notes = FakeNotesManager(tmp_path)
    history = ConversationHistory()
    app = make_builtin_app(tmp_path, RestorableAgentLoop(), history, notes=notes)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "/notes"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        prompt_input.value = "/notes paths"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        text = render_log_text(app.query_one("#chat-log", RichLog)).replace(
            "\n",
            "",
        )
        assert "用户偏好" in text and "concise" in text
        assert "纠正反馈" in text and "keep exact names" in text
        assert "项目知识" in text and "entry src/main.py" in text
        assert "参考资料" in text and "docs/spec.md" in text
        assert str(notes.paths.user) in text
        assert str(notes.paths.project) in text

    assert history.snapshot() == []


@pytest.mark.asyncio
async def test_notes_clear_requires_scope_and_path_confirmation(
    tmp_path: Path,
) -> None:
    notes = FakeNotesManager(tmp_path)
    history = ConversationHistory()
    app = make_builtin_app(tmp_path, RestorableAgentLoop(), history, notes=notes)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "/notes clear project"
        await pilot.press("enter")
        await pilot.pause()

        screen_text = "\n".join(
            str(widget.render()) for widget in app.screen.query(Static)
        )
        assert "scope：project" in screen_text
        assert str(notes.paths.project) in screen_text
        assert notes.clear_calls == []

        await pilot.click("#confirm-command")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert notes.clear_calls == ["project"]
        assert "笔记已清空" in str(
            app.query_one("#status", Static).render()
        )

    assert history.snapshot() == []


@pytest.mark.asyncio
async def test_final_response_counts_one_success_for_notes(
    tmp_path: Path,
) -> None:
    notes = FakeNotesManager(tmp_path)
    loop = GatedAgentLoop()
    history = ConversationHistory()
    app = ChatApp(
        loop,
        history,
        provider_id="provider",
        model="model",
        notes_manager=notes,  # type: ignore[arg-type]
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "successful request"
        await pilot.press("enter")
        await loop.started.wait()
        assert notes.successes == 0
        loop.release.set()
        await app.workers.wait_for_complete()

    assert notes.successes == 1


@pytest.mark.asyncio
async def test_case_insensitive_notes_alias_is_consumed_locally(
    tmp_path: Path,
) -> None:
    notes = FakeNotesManager(tmp_path)
    loop = GatedAgentLoop()
    history = ConversationHistory()
    app = make_builtin_app(tmp_path, loop, history, notes=notes)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "/Notes"
        await pilot.press("enter")
        await app.workers.wait_for_complete()

    assert history.snapshot() == []
    assert loop.started.is_set() is False


@pytest.mark.asyncio
async def test_escape_cancels_manual_compaction(tmp_path: Path) -> None:
    loop = GatedManualCompactionAgentLoop()
    app = make_builtin_app(tmp_path, loop, ConversationHistory())

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "/compact"
        await pilot.press("enter")
        await loop.started.wait()
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert prompt_input.disabled is False
        assert "context_compaction_cancelled" in str(
            app.query_one("#status", Static).render()
        )

    assert loop.cancelled is True


@pytest.mark.asyncio
async def test_app_renders_agent_error_without_adding_error_to_history() -> None:
    history = ConversationHistory()
    app = make_app(ErrorAgentLoop(), history)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "触发错误"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        status = app.query_one("#status", Static)
        assert "错误：模拟失败" in str(status.render())
        assert history.snapshot() == [
            ChatMessage(role="user", content="触发错误")
        ]


@pytest.mark.asyncio
async def test_app_renders_tool_start_and_result_events() -> None:
    loop = GatedToolEventAgentLoop()
    app = make_app(loop)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "读取说明"
        await pilot.press("enter")
        await loop.tool_started.wait()
        await pilot.pause()

        status = app.query_one("#status", Static)
        assert "执行工具：read_file" in str(status.render())
        assert "Assistant → Tool read_file" in render_log_text(
            app.query_one("#chat-log", RichLog)
        )

        loop.release_tool.set()
        await loop.result_emitted.wait()
        await pilot.pause()

        assert "工具 read_file 完成" in str(status.render())
        log_text = render_log_text(app.query_one("#chat-log", RichLog))
        assert "Tool result:" in log_text
        assert '"tool_name":"read_file","success":true' in log_text

        loop.release_result.set()
        await app.workers.wait_for_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("button_id", "expected"),
    [
        ("#allow-once", "allow_once"),
        ("#allow-session", "allow_session"),
        ("#allow-permanent", "allow_permanent"),
        ("#reject-tool", "reject"),
    ],
)
async def test_tool_approval_card_resolves_context(
    button_id: str,
    expected: str,
) -> None:
    loop = ToolApprovalAgentLoop()
    app = make_app(loop)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "执行写工具"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click(button_id)
        await app.workers.wait_for_complete()

    assert loop.decision == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("button_id", "expected"),
    [
        ("#execute-current", "execute_current"),
        ("#reject-plan", "reject"),
    ],
)
async def test_plan_approval_card_resolves_simple_decisions(
    button_id: str,
    expected: str,
) -> None:
    loop = PlanApprovalAgentLoop()
    app = make_app(loop)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "规划任务"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click(button_id)
        await app.workers.wait_for_complete()

    assert loop.resolution == PlanApprovalResolution(expected)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_plan_change_card_requires_and_returns_feedback() -> None:
    loop = PlanApprovalAgentLoop()
    app = make_app(loop)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "规划任务"
        await pilot.press("enter")
        await pilot.pause()

        clicked = await pilot.click("#request-changes")
        assert clicked is True
        feedback = app.screen.query_one("#plan-feedback", Input)
        assert feedback.placeholder == "必须填写修改意见"

        feedback.value = "补充测试步骤"
        await pilot.pause(0.21)
        assert feedback.value == "补充测试步骤"
        clicked = await pilot.click("#request-changes")
        assert clicked is True
        assert feedback.value == "补充测试步骤"
        await pilot.pause()
        assert loop.resolution == PlanApprovalResolution(
            "request_changes",
            "补充测试步骤",
        )
        await app.workers.wait_for_complete()

    assert loop.resolution == PlanApprovalResolution(
        "request_changes",
        "补充测试步骤",
    )


@pytest.mark.asyncio
async def test_final_round_plan_card_disables_execute_and_changes() -> None:
    loop = PlanApprovalAgentLoop(
        can_execute=False,
        can_request_changes=False,
    )
    app = make_app(loop)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "规划任务"
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.query_one("#execute-current", Button).disabled is True
        assert app.screen.query_one("#request-changes", Button).disabled is True
        assert "当前请求已达到 15 轮上限" in str(
            app.screen.query_one("#round-limit-message", Static).render()
        )

        await pilot.click("#reject-plan")
        await app.workers.wait_for_complete()

    assert loop.resolution == PlanApprovalResolution("reject")


@pytest.mark.asyncio
async def test_escape_cancels_active_approval_wait() -> None:
    loop = ToolApprovalAgentLoop()
    app = make_app(loop)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "执行写工具"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await app.workers.wait_for_complete()

        assert prompt_input.disabled is False
        assert prompt_input.has_focus is True

    assert loop.cancelled is True


@pytest.mark.asyncio
async def test_escape_detaches_foreground_worker_before_parent_cancel() -> None:
    events: list[str] = []

    class RecordingWorkerManager:
        async def detach_foreground(self) -> str:
            events.append("detach")
            return "a" * 32

    class RecordingLoop(ToolApprovalAgentLoop):
        async def run(self, message, history, *, plan_only, context):
            async for event in super().run(
                message,
                history,
                plan_only=plan_only,
                context=context,
            ):
                yield event
            if context.cancelled:
                events.append("cancel")

    loop = RecordingLoop()
    app = ChatApp(
        loop,  # type: ignore[arg-type]
        ConversationHistory(),
        provider_id="provider",
        model="model",
        worker_manager=RecordingWorkerManager(),  # type: ignore[arg-type]
    )

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        prompt_input.value = "执行写工具"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await app.workers.wait_for_complete()

    assert events == ["detach", "cancel"]
