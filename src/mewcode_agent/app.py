"""Textual terminal interface for the event-driven ReAct agent."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RichLog, Static, Switch
from textual.worker import Worker

from mewcode_agent.agent import (
    AgentEvent,
    AgentLoop,
    AgentRunContext,
    ContextCompactionCompletedEvent,
    ContextCompactionStartedEvent,
    ContextCompactionWarningEvent,
    FinalResponseEvent,
    ModelTextEvent,
    ModelThinkingEvent,
    PlanApprovalRequestedEvent,
    PlanApprovalResolution,
    RoundStartedEvent,
    RunCancelledEvent,
    RunErrorEvent,
    ToolApprovalDecision,
    ToolApprovalRequestedEvent,
    ToolCallStartedEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from mewcode_agent.compaction import ContextCompactionError
from mewcode_agent.history import ConversationHistory
from mewcode_agent.notes import (
    NoteClearTarget,
    NoteCommand,
    NotesError,
    NotesManager,
    parse_note_command,
)
from mewcode_agent.sessions import (
    SessionCommand,
    SessionDeleteTarget,
    SessionError,
    SessionManager,
    SessionRecovery,
    parse_session_command,
)


class ToolApprovalScreen(ModalScreen[ToolApprovalDecision | None]):
    """Ask whether one write or command tool call may execute."""

    BINDINGS = [("escape", "cancel_run", "取消当前请求")]

    CSS = """
    ToolApprovalScreen {
        align: center middle;
        background: $background 60%;
    }

    #tool-approval-card {
        width: 72;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }

    #tool-approval-actions {
        height: auto;
        margin-top: 1;
    }

    #tool-approval-actions Button {
        margin-right: 1;
    }
    """

    def __init__(self, event: ToolApprovalRequestedEvent) -> None:
        super().__init__()
        self._event = event

    def compose(self) -> ComposeResult:
        with Vertical(id="tool-approval-card"):
            yield Static("工具执行审批")
            yield Static(f"工具：{self._event.tool_name}")
            yield Static(f"类别：{self._event.category}")
            yield Static(f"原因：{self._event.reason_code}")
            yield Static(f"参数：{self._event.arguments_json}")
            with Horizontal(id="tool-approval-actions"):
                yield Button(
                    "仅允许这一次",
                    id="allow-once",
                    variant="warning",
                )
                yield Button(
                    "本会话允许",
                    id="allow-session",
                    variant="primary",
                )
                yield Button(
                    "永久允许",
                    id="allow-permanent",
                    variant="success",
                )
                yield Button("拒绝", id="reject-tool", variant="error")

    @on(Button.Pressed, "#allow-once")
    def allow_once(self) -> None:
        self.dismiss("allow_once")

    @on(Button.Pressed, "#allow-session")
    def allow_session(self) -> None:
        self.dismiss("allow_session")

    @on(Button.Pressed, "#allow-permanent")
    def allow_permanent(self) -> None:
        self.dismiss("allow_permanent")

    @on(Button.Pressed, "#reject-tool")
    def reject(self) -> None:
        self.dismiss("reject")

    def action_cancel_run(self) -> None:
        self.app.action_cancel_run()
        self.dismiss(None)


class PlanApprovalScreen(ModalScreen[PlanApprovalResolution | None]):
    """Show the generated plan and collect the user's decision."""

    BINDINGS = [("escape", "cancel_run", "取消当前请求")]

    CSS = """
    PlanApprovalScreen {
        align: center middle;
        background: $background 60%;
    }

    #plan-approval-card {
        width: 80;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }

    #plan-content {
        max-height: 16;
        overflow-y: auto;
        margin: 1 0;
    }

    #round-limit-message {
        color: $warning;
        margin-bottom: 1;
    }

    #plan-feedback {
        margin-bottom: 1;
    }

    #plan-approval-actions {
        height: auto;
    }

    #plan-approval-actions Button {
        margin-right: 1;
    }
    """

    def __init__(self, event: PlanApprovalRequestedEvent) -> None:
        super().__init__()
        self._event = event

    def compose(self) -> ComposeResult:
        limit_message = (
            ""
            if self._event.can_execute
            or self._event.can_request_changes
            else "当前请求已达到 15 轮上限，请开启新请求执行该计划。"
        )
        with Vertical(id="plan-approval-card"):
            yield Static("计划审批")
            yield Static(self._event.plan, id="plan-content")
            yield Static(limit_message, id="round-limit-message")
            yield Input(
                id="plan-feedback",
                placeholder="需要修改时填写意见",
                disabled=not self._event.can_request_changes,
            )
            with Horizontal(id="plan-approval-actions"):
                yield Button(
                    "执行当前计划",
                    id="execute-current",
                    variant="success",
                    disabled=not self._event.can_execute,
                )
                yield Button(
                    "要求修改",
                    id="request-changes",
                    variant="warning",
                    disabled=not self._event.can_request_changes,
                )
                yield Button("拒绝", id="reject-plan", variant="error")

    @on(Button.Pressed, "#execute-current")
    def execute_current(self) -> None:
        self.dismiss(PlanApprovalResolution("execute_current"))

    @on(Button.Pressed, "#request-changes")
    def request_changes(self) -> None:
        feedback_input = self.query_one("#plan-feedback", Input)
        feedback = feedback_input.value.strip()
        if not feedback:
            feedback_input.placeholder = "必须填写修改意见"
            feedback_input.focus()
            return
        self.dismiss(PlanApprovalResolution("request_changes", feedback))

    @on(Button.Pressed, "#reject-plan")
    def reject(self) -> None:
        self.dismiss(PlanApprovalResolution("reject"))

    def action_cancel_run(self) -> None:
        self.app.action_cancel_run()
        self.dismiss(None)


class SessionDeleteScreen(ModalScreen[bool]):
    """Require explicit confirmation before deleting one saved session."""

    BINDINGS = [("escape", "cancel_delete", "取消删除")]

    CSS = """
    SessionDeleteScreen {
        align: center middle;
        background: $background 60%;
    }

    #session-delete-card {
        width: 80;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $error;
        background: $surface;
    }

    #session-delete-actions {
        height: auto;
        margin-top: 1;
    }

    #session-delete-actions Button {
        margin-right: 1;
    }
    """

    def __init__(self, target: SessionDeleteTarget) -> None:
        super().__init__()
        self._target = target

    def compose(self) -> ComposeResult:
        with Vertical(id="session-delete-card"):
            yield Static("删除会话（不可恢复）")
            yield Static(f"session ID：{self._target.session_id}")
            yield Static(f"标题：{self._target.title}")
            yield Static(f"路径：{self._target.path}")
            with Horizontal(id="session-delete-actions"):
                yield Button(
                    "确认删除",
                    id="confirm-session-delete",
                    variant="error",
                )
                yield Button(
                    "取消",
                    id="cancel-session-delete",
                    variant="primary",
                )

    @on(Button.Pressed, "#confirm-session-delete")
    def confirm_delete(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-session-delete")
    def cancel_delete(self) -> None:
        self.dismiss(False)

    def action_cancel_delete(self) -> None:
        self.dismiss(False)


class NoteClearScreen(ModalScreen[bool]):
    """Require explicit confirmation before clearing one note scope."""

    BINDINGS = [("escape", "cancel_clear", "取消清空")]

    CSS = """
    NoteClearScreen {
        align: center middle;
        background: $background 60%;
    }

    #note-clear-card {
        width: 80;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }

    #note-clear-actions {
        height: auto;
        margin-top: 1;
    }

    #note-clear-actions Button {
        margin-right: 1;
    }
    """

    def __init__(self, target: NoteClearTarget) -> None:
        super().__init__()
        self._target = target

    def compose(self) -> ComposeResult:
        with Vertical(id="note-clear-card"):
            yield Static("清空笔记")
            yield Static(f"scope：{self._target.scope}")
            yield Static(f"路径：{self._target.path}")
            with Horizontal(id="note-clear-actions"):
                yield Button(
                    "确认清空",
                    id="confirm-note-clear",
                    variant="warning",
                )
                yield Button(
                    "取消",
                    id="cancel-note-clear",
                    variant="primary",
                )

    @on(Button.Pressed, "#confirm-note-clear")
    def confirm_clear(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-note-clear")
    def cancel_clear(self) -> None:
        self.dismiss(False)

    def action_cancel_clear(self) -> None:
        self.dismiss(False)


class ChatApp(App[None]):
    """Consume AgentLoop events in a single-session terminal UI."""

    BINDINGS = [("escape", "cancel_run", "取消当前请求")]

    CSS = """
    Screen {
        layout: vertical;
    }

    #chat-log {
        height: 1fr;
        padding: 1 2;
    }

    #status {
        height: 1;
        padding: 0 2;
        color: $text-muted;
    }

    #mode-controls {
        height: 3;
        padding: 0 2;
        align-vertical: middle;
    }

    #mode-controls Static {
        width: auto;
        margin-right: 1;
    }

    #prompt-input {
        dock: bottom;
    }
    """

    def __init__(
        self,
        agent_loop: AgentLoop,
        history: ConversationHistory,
        *,
        provider_id: str,
        model: str,
        session_manager: SessionManager | None = None,
        session_activator: Callable[[SessionRecovery], None] | None = None,
        notes_manager: NotesManager | None = None,
    ) -> None:
        super().__init__()
        if (session_manager is None) != (session_activator is None):
            raise ValueError(
                "session_manager 与 session_activator 必须同时提供"
            )
        self.agent_loop = agent_loop
        self.history = history
        self.provider_id = provider_id
        self.model = model
        self.session_manager = session_manager
        self._session_activator = session_activator
        self.notes_manager = notes_manager
        self.active_response = ""
        self.active_thinking = ""
        self._command_output: list[str] = []
        self._active_context: AgentRunContext | None = None
        self._active_compaction_worker: Worker[None] | None = None
        self._active_session_worker: Worker[None] | None = None
        self._active_note_worker: Worker[None] | None = None

    def compose(self) -> ComposeResult:
        yield RichLog(id="chat-log", wrap=True, markup=False)
        yield Static(id="status")
        with Horizontal(id="mode-controls"):
            yield Static("仅规划")
            yield Switch(id="plan-only-switch")
        yield Input(
            id="prompt-input",
            placeholder="输入消息并按 Enter 发送",
        )

    def on_mount(self) -> None:
        self._set_status("就绪")
        self.query_one("#prompt-input", Input).focus()

    def _set_status(self, state: str) -> None:
        self.query_one("#status", Static).update(
            f"{self.provider_id} | {self.model} | {state}"
        )

    def _clear_active_output(self) -> None:
        self.active_response = ""
        self.active_thinking = ""

    def _render_transcript(self) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.clear()
        for message in self.history.snapshot():
            if message.role == "user":
                log.write(f"You: {message.content}")
            elif message.role == "assistant":
                if message.content:
                    log.write(f"Assistant: {message.content}")
                for tool_call in message.tool_calls:
                    log.write(
                        f"Assistant → Tool {tool_call.name}: "
                        f"{tool_call.arguments_json}"
                    )
            else:
                log.write(f"Tool result: {message.content}")
        if self.active_thinking:
            log.write(f"Thinking: {self.active_thinking}")
        if self.active_response:
            log.write(f"Assistant: {self.active_response}")
        for line in self._command_output:
            log.write(f"MewCode: {line}")

    def _show_command_output(self, *lines: str) -> None:
        self._command_output.extend(lines)
        self._render_transcript()

    @on(Input.Submitted, "#prompt-input")
    def submit_prompt(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        prompt_input = self.query_one("#prompt-input", Input)
        prompt_input.value = ""
        if not prompt or prompt_input.disabled:
            return

        plan_switch = self.query_one("#plan-only-switch", Switch)
        note_command = (
            parse_note_command(prompt)
            if self.notes_manager is not None
            else None
        )
        if note_command is not None:
            self._clear_active_output()
            prompt_input.disabled = True
            plan_switch.disabled = True
            self._set_status("正在处理笔记命令")
            self._active_note_worker = self.run_note_command(note_command)
            return
        session_command = (
            parse_session_command(prompt)
            if self.session_manager is not None
            else None
        )
        if session_command is not None:
            self._clear_active_output()
            prompt_input.disabled = True
            plan_switch.disabled = True
            self._set_status("正在处理会话命令")
            self._active_session_worker = self.run_session_command(
                session_command
            )
            return
        if prompt == "/compact":
            self._clear_active_output()
            prompt_input.disabled = True
            plan_switch.disabled = True
            self._set_status("正在压缩上下文")
            self._active_compaction_worker = self.compact_context()
            return

        plan_only = plan_switch.value
        self._clear_active_output()
        prompt_input.disabled = True
        plan_switch.disabled = True
        self._set_status("生成中")
        self.stream_response(prompt, plan_only)

    @work(exclusive=True, exit_on_error=False)
    async def stream_response(self, prompt: str, plan_only: bool) -> None:
        prompt_input = self.query_one("#prompt-input", Input)
        plan_switch = self.query_one("#plan-only-switch", Switch)
        context = AgentRunContext()
        self._active_context = context
        try:
            async for event in self.agent_loop.run(
                prompt,
                self.history,
                plan_only=plan_only,
                context=context,
            ):
                await self._handle_agent_event(event, context)
        except SessionError as exc:
            self._clear_active_output()
            self._render_transcript()
            self._set_status(
                f"错误：{exc.message}（{exc.code}）"
            )
        except Exception:
            self._clear_active_output()
            self._render_transcript()
            self._set_status("错误：Agent 运行失败")
        finally:
            self._active_context = None
            prompt_input.disabled = False
            plan_switch.disabled = False
            prompt_input.focus()

    @work(exclusive=True, exit_on_error=False)
    async def run_session_command(self, command: SessionCommand) -> None:
        prompt_input = self.query_one("#prompt-input", Input)
        plan_switch = self.query_one("#plan-only-switch", Switch)
        manager = self.session_manager
        assert manager is not None
        try:
            if command.kind == "list":
                metas = await asyncio.to_thread(manager.list_sessions)
                if metas:
                    self._show_command_output(
                        *(
                            f"{meta.session_id} | {meta.updated_at} | "
                            f"{meta.title} | {meta.summary}"
                            for meta in metas
                        )
                    )
                    self._set_status(f"已列出 {len(metas)} 个会话")
                else:
                    self._show_command_output("当前项目没有已保存会话")
                    self._set_status("会话列表为空")
                return

            assert command.session_id is not None
            if command.kind == "path":
                path = await asyncio.to_thread(
                    manager.session_path,
                    command.session_id,
                )
                self._show_command_output(str(path))
                self._set_status("已显示会话路径")
                return
            if command.kind == "delete":
                target = await asyncio.to_thread(
                    manager.prepare_delete,
                    command.session_id,
                )
                confirmed = await self.push_screen_wait(
                    SessionDeleteScreen(target)
                )
                if not confirmed:
                    self._set_status("已取消删除会话")
                    return
                await asyncio.to_thread(manager.delete, target)
                self._show_command_output(
                    f"已删除会话 {target.session_id}：{target.path}"
                )
                self._set_status("会话已删除")
                return

            assert command.kind == "resume"
            assert self._session_activator is not None
            if self.notes_manager is not None:
                await self.notes_manager.wait_until_idle()
            recovery = await manager.resume_async(
                command.session_id,
                activate=self._session_activator,
            )
            self._clear_active_output()
            self._render_transcript()
            try:
                preparation = await self.agent_loop.prepare_restored_history(
                    self.history
                )
            except ContextCompactionError as exc:
                self._render_transcript()
                self._set_status(
                    "会话已恢复；恢复上下文处理失败："
                    f"{exc.code}"
                )
                return
            details = (
                f"消息={len(recovery.messages)}，"
                f"修复={'是' if recovery.repaired else '否'}，"
                f"诊断={len(recovery.diagnostics)}"
            )
            if preparation is not None and preparation.summary_changed:
                details += "，已执行恢复压缩"
            self._set_status(f"会话已恢复：{details}")
        except SessionError as exc:
            self._set_status(f"会话命令失败：{exc.message}（{exc.code}）")
        except Exception:
            self._set_status(
                "会话命令失败：会话恢复或运行时重置失败"
                "（session_resume_failed）"
            )
        finally:
            self._active_session_worker = None
            prompt_input.disabled = False
            plan_switch.disabled = False
            prompt_input.focus()

    @work(exclusive=True, exit_on_error=False)
    async def run_note_command(self, command: NoteCommand) -> None:
        prompt_input = self.query_one("#prompt-input", Input)
        plan_switch = self.query_one("#plan-only-switch", Switch)
        manager = self.notes_manager
        assert manager is not None
        try:
            if command.kind == "show":
                snapshot = manager.snapshot

                def section(title: str, entries: tuple[str, ...]) -> list[str]:
                    return [title, *(f"- {entry}" for entry in entries)] if entries else [title, "(空)"]

                self._show_command_output(
                    *section("用户偏好", snapshot.user_preferences),
                    *section("纠正反馈", snapshot.correction_feedback),
                    *section("项目知识", snapshot.project_knowledge),
                    *section("参考资料", snapshot.references),
                )
                self._set_status("已显示当前笔记")
                return
            if command.kind == "paths":
                self._show_command_output(
                    f"user: {manager.paths.user}",
                    f"project: {manager.paths.project}",
                )
                self._set_status("已显示笔记路径")
                return
            scope = "user" if command.kind == "clear_user" else "project"
            target = manager.clear_target(scope)
            confirmed = await self.push_screen_wait(NoteClearScreen(target))
            if not confirmed:
                self._set_status("已取消清空笔记")
                return
            await manager.clear(scope)
            self._show_command_output(
                f"已清空 {scope} 笔记：{target.path}"
            )
            self._set_status("笔记已清空")
        except NotesError as exc:
            self._set_status(f"笔记命令失败：{exc.message}（{exc.code}）")
        except Exception:
            self._set_status(
                "笔记命令失败：笔记操作发生未预期错误"
                "（notes_write_failed）"
            )
        finally:
            self._active_note_worker = None
            prompt_input.disabled = False
            plan_switch.disabled = False
            prompt_input.focus()

    @work(exclusive=True, exit_on_error=False)
    async def compact_context(self) -> None:
        prompt_input = self.query_one("#prompt-input", Input)
        plan_switch = self.query_one("#plan-only-switch", Switch)
        try:
            result = await self.agent_loop.compact_history(self.history)
        except asyncio.CancelledError:
            self._set_status("已取消：context_compaction_cancelled")
            raise
        except ContextCompactionError as exc:
            self._set_status(f"压缩失败：{exc.message}（{exc.code}）")
        except Exception:
            self._set_status("压缩失败：上下文压缩发生未预期错误")
        else:
            if not result.changed:
                self._set_status("没有可压缩的历史")
            else:
                reduction = result.estimate_before - result.estimate_after
                self._set_status(
                    "上下文压缩完成："
                    f"generation={result.generation}，"
                    f"覆盖消息={result.covered_history_end}，"
                    f"估算减少={reduction}"
                )
        finally:
            self._active_compaction_worker = None
            prompt_input.disabled = False
            plan_switch.disabled = False
            prompt_input.focus()

    async def _handle_agent_event(
        self,
        event: AgentEvent,
        context: AgentRunContext,
    ) -> None:
        if isinstance(event, UserMessageEvent):
            self._render_transcript()
        elif isinstance(event, RoundStartedEvent):
            self._clear_active_output()
            self._render_transcript()
            mode = "规划" if event.mode == "planning" else "执行"
            self._set_status(
                f"{mode}中（模型轮 {event.round_number}/{event.max_rounds}）"
            )
        elif isinstance(event, ContextCompactionStartedEvent):
            self._set_status(
                "正在自动压缩上下文："
                f"generation={event.generation}，"
                f"覆盖消息={event.covered_messages}"
            )
        elif isinstance(event, ContextCompactionCompletedEvent):
            reduction = event.estimate_before - event.estimate_after
            self._set_status(
                "自动上下文压缩完成："
                f"generation={event.generation}，"
                f"覆盖消息={event.covered_messages}，"
                f"估算减少={reduction}"
            )
        elif isinstance(event, ContextCompactionWarningEvent):
            self._set_status(f"上下文压缩警告：{event.error_code}")
        elif isinstance(event, ModelThinkingEvent):
            self.active_thinking += event.text
            self._render_transcript()
        elif isinstance(event, ModelTextEvent):
            self.active_response += event.text
            self._render_transcript()
        elif isinstance(event, ToolApprovalRequestedEvent):
            decision = await self.push_screen_wait(
                ToolApprovalScreen(event)
            )
            if decision is not None:
                context.resolve_tool_approval(event.request_id, decision)
        elif isinstance(event, PlanApprovalRequestedEvent):
            self._clear_active_output()
            self._render_transcript()
            resolution = await self.push_screen_wait(
                PlanApprovalScreen(event)
            )
            if resolution is not None:
                context.resolve_plan_approval(
                    event.request_id,
                    resolution.decision,
                    feedback=resolution.feedback,
                )
        elif isinstance(event, ToolCallStartedEvent):
            self._clear_active_output()
            self._render_transcript()
            self._set_status(f"执行工具：{event.tool_name}")
        elif isinstance(event, ToolResultEvent):
            self._clear_active_output()
            self._render_transcript()
            result_state = "完成" if event.result.success else "失败"
            self._set_status(f"工具 {event.result.tool_name} {result_state}")
        elif isinstance(event, FinalResponseEvent):
            self._clear_active_output()
            self._render_transcript()
            self._set_status("就绪")
            if self.notes_manager is not None:
                self.notes_manager.record_successful_request()
        elif isinstance(event, RunErrorEvent):
            self._clear_active_output()
            self._render_transcript()
            self._set_status(f"错误：{event.message}")
        elif isinstance(event, RunCancelledEvent):
            self._clear_active_output()
            self._render_transcript()
            self._set_status(f"已取消：{event.reason}")

    def action_cancel_run(self) -> None:
        if self._active_context is not None:
            self._active_context.cancel()
        elif self._active_compaction_worker is not None:
            self._active_compaction_worker.cancel()
