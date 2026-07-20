"""Textual terminal interface for the event-driven ReAct agent."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    OptionList,
    RichLog,
    Static,
    Switch,
)
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
from mewcode_agent.commands import (
    CommandController,
    CommandError,
    CommandMode,
    CommandRegistry,
    ConfirmationRequest,
)
from mewcode_agent.history import ConversationHistory
from mewcode_agent.notes import NotesManager
from mewcode_agent.sessions import SessionError

if TYPE_CHECKING:
    from mewcode_agent.workers import WorkerManager


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


class CommandConfirmationScreen(ModalScreen[bool]):
    """Render one generic command confirmation request."""

    BINDINGS = [("escape", "cancel_confirmation", "取消")]

    CSS = """
    CommandConfirmationScreen {
        align: center middle;
        background: $background 60%;
    }

    #command-confirmation-card {
        width: 80;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }

    #command-confirmation-actions {
        height: auto;
        margin-top: 1;
    }

    #command-confirmation-actions Button {
        margin-right: 1;
    }
    """

    def __init__(self, request: ConfirmationRequest) -> None:
        super().__init__()
        self._request = request

    def compose(self) -> ComposeResult:
        with Vertical(id="command-confirmation-card"):
            yield Static(self._request.title)
            for name, value in self._request.fields:
                yield Static(f"{name}：{value}")
            with Horizontal(id="command-confirmation-actions"):
                yield Button(
                    "确认",
                    id="confirm-command",
                    variant=(
                        "error" if self._request.destructive else "warning"
                    ),
                )
                yield Button(
                    "取消",
                    id="cancel-command",
                    variant="primary",
                )

    @on(Button.Pressed, "#confirm-command")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-command")
    def cancel(self) -> None:
        self.dismiss(False)

    def action_cancel_confirmation(self) -> None:
        self.dismiss(False)


class CommandCompletionScreen(ModalScreen[str | None]):
    """Keyboard-selectable popup for public command completions."""

    BINDINGS = [("escape", "cancel_completion", "取消补全")]

    CSS = """
    CommandCompletionScreen {
        align: center bottom;
        background: $background 20%;
    }

    #command-completions {
        width: 50;
        height: auto;
        max-height: 12;
        margin-bottom: 3;
    }
    """

    def __init__(self, candidates: tuple[str, ...]) -> None:
        if len(candidates) < 2:
            raise ValueError("补全弹窗至少需要两个候选")
        super().__init__()
        self._candidates = candidates

    def compose(self) -> ComposeResult:
        yield OptionList(
            *(f"/{candidate}" for candidate in self._candidates),
            id="command-completions",
        )

    @on(OptionList.OptionSelected, "#command-completions")
    def select_option(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._candidates[event.option_index])

    def action_cancel_completion(self) -> None:
        self.dismiss(None)


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
        command_registry: CommandRegistry | None = None,
        notes_manager: NotesManager | None = None,
        worker_manager: WorkerManager | None = None,
    ) -> None:
        super().__init__()
        self.agent_loop = agent_loop
        self.history = history
        self.provider_id = provider_id
        self.model = model
        self.notes_manager = notes_manager
        self.worker_manager = worker_manager
        self.command_registry = command_registry or CommandRegistry()
        if not self.command_registry.frozen:
            self.command_registry.freeze()
        self.command_controller = CommandController(
            self.command_registry,
            self,
        )
        self.active_response = ""
        self.active_thinking = ""
        self._command_output: list[str] = []
        self._default_mode: CommandMode = "execute"
        self._status_state = "就绪"
        self._active_context: AgentRunContext | None = None
        self._active_input_worker: Worker[None] | None = None
        self._restart_target: Path | None = None

    @property
    def restart_target(self) -> Path | None:
        return self._restart_target

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
        self._status_state = state
        hints = " ".join(self.command_registry.status_hints())
        suffix = f" | {hints}" if hints else ""
        self.query_one("#status", Static).update(
            f"{self.provider_id} | {self.model} | "
            f"mode={self._default_mode} | {state}{suffix}"
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

    async def show_system_message(self, lines: tuple[str, ...]) -> None:
        if not isinstance(lines, tuple) or any(
            not isinstance(line, str) for line in lines
        ):
            raise ValueError("命令输出必须是字符串 tuple")
        self._command_output.extend(lines)
        self._render_transcript()

    async def request_confirmation(
        self,
        request: ConfirmationRequest,
    ) -> bool:
        try:
            result = await self.push_screen_wait(
                CommandConfirmationScreen(request)
            )
        except Exception as exc:
            raise CommandError("command_confirmation_failed") from exc
        return bool(result)

    async def send_user_message(
        self,
        message: str,
        *,
        mode: CommandMode,
    ) -> None:
        await self._run_agent_message(message, mode=mode)

    def get_default_mode(self) -> CommandMode:
        return self._default_mode

    def set_default_mode(self, mode: CommandMode) -> None:
        if mode not in ("plan", "execute"):
            raise ValueError("mode 必须为 plan 或 execute")
        self._default_mode = mode
        switch = self.query_one("#plan-only-switch", Switch)
        switch.value = mode == "plan"

    def clear_transcript(self) -> None:
        self._clear_active_output()
        self._command_output.clear()
        self._render_transcript()

    def refresh_status(self, state: str) -> None:
        self._set_status(state)

    def request_workspace_restart(self, target: Path) -> None:
        if self._active_context is not None:
            raise CommandError("command_unavailable")
        if not isinstance(target, Path) or not target.is_absolute():
            raise ValueError("restart target 必须是绝对 Path")
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CommandError("command_unavailable") from exc
        if not resolved.is_dir():
            raise CommandError("command_unavailable")
        self._restart_target = resolved
        self.exit()

    @on(Switch.Changed, "#plan-only-switch")
    def update_default_mode(self, event: Switch.Changed) -> None:
        self._default_mode = "plan" if event.value else "execute"
        self._set_status(self._status_state)

    @on(Input.Submitted, "#prompt-input")
    def submit_prompt(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        prompt_input = self.query_one("#prompt-input", Input)
        prompt_input.value = ""
        if not prompt or prompt_input.disabled:
            return
        self._clear_active_output()
        prompt_input.disabled = True
        self.query_one("#plan-only-switch", Switch).disabled = True
        self._set_status("正在处理输入")
        self._active_input_worker = self.process_input(prompt)

    @work(exclusive=True, exit_on_error=False)
    async def process_input(self, prompt: str) -> None:
        prompt_input = self.query_one("#prompt-input", Input)
        plan_switch = self.query_one("#plan-only-switch", Switch)
        try:
            result = await self.command_controller.dispatch(prompt)
            if not result.consumed:
                await self._run_agent_message(
                    prompt,
                    mode=self._default_mode,
                )
            elif self._status_state == "正在处理输入":
                self._set_status("就绪")
        finally:
            self._active_input_worker = None
            prompt_input.disabled = False
            plan_switch.disabled = False
            prompt_input.focus()

    async def _run_agent_message(
        self,
        prompt: str,
        *,
        mode: CommandMode,
    ) -> None:
        context = AgentRunContext()
        self._active_context = context
        self._clear_active_output()
        self._set_status("生成中")
        try:
            async for event in self.agent_loop.run(
                prompt,
                self.history,
                plan_only=mode == "plan",
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

    def on_key(self, event: events.Key) -> None:
        if event.key != "tab" or isinstance(
            self.screen,
            CommandCompletionScreen,
        ):
            return
        prompt_input = self.query_one("#prompt-input", Input)
        if (
            prompt_input.disabled
            or not prompt_input.has_focus
            or prompt_input.cursor_position != len(prompt_input.value)
        ):
            return
        candidate_text = prompt_input.value.lstrip()
        if not candidate_text.startswith("/") or " " in candidate_text[1:]:
            return
        event.prevent_default()
        event.stop()
        candidates = self.command_registry.completion_candidates(
            candidate_text[1:]
        )
        if not candidates:
            self._set_status("没有匹配的命令；输入 /help 查看帮助")
            return
        if len(candidates) == 1:
            self._apply_completion(candidates[0])
            return
        self.push_screen(
            CommandCompletionScreen(candidates),
            self._apply_completion,
        )

    def _apply_completion(self, candidate: str | None) -> None:
        prompt_input = self.query_one("#prompt-input", Input)
        if candidate is not None:
            prompt_input.value = f"/{candidate} "
            prompt_input.cursor_position = len(prompt_input.value)
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
            context = self._active_context
            if self.worker_manager is None:
                context.cancel()
            else:
                asyncio.create_task(
                    self._detach_worker_then_cancel(context)
                )
        elif self._active_input_worker is not None:
            self._active_input_worker.cancel()

    async def _detach_worker_then_cancel(
        self,
        context: AgentRunContext,
    ) -> None:
        assert self.worker_manager is not None
        try:
            await self.worker_manager.detach_foreground()
        finally:
            context.cancel()
