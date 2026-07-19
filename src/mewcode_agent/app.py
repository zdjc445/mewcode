"""Textual terminal interface for the event-driven ReAct agent."""

from __future__ import annotations

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RichLog, Static, Switch

from mewcode_agent.agent import (
    AgentEvent,
    AgentLoop,
    AgentRunContext,
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
from mewcode_agent.history import ConversationHistory


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
    ) -> None:
        super().__init__()
        self.agent_loop = agent_loop
        self.history = history
        self.provider_id = provider_id
        self.model = model
        self.active_response = ""
        self.active_thinking = ""
        self._active_context: AgentRunContext | None = None

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

    @on(Input.Submitted, "#prompt-input")
    def submit_prompt(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        prompt_input = self.query_one("#prompt-input", Input)
        prompt_input.value = ""
        if not prompt or prompt_input.disabled:
            return

        plan_switch = self.query_one("#plan-only-switch", Switch)
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
        except Exception:
            self._clear_active_output()
            self._render_transcript()
            self._set_status("错误：Agent 运行失败")
        finally:
            self._active_context = None
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
