"""Textual terminal chat interface."""

from __future__ import annotations

from textual import on, work
from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog, Static

from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ToolCall
from mewcode_agent.providers.base import LLMProvider, ProviderError
from mewcode_agent.tools.base import ToolResult
from mewcode_agent.tools.registry import ToolRegistry

MAX_TOOL_CALLS_PER_TURN = 10


class ChatApp(App[None]):
    """A single-session, streaming terminal chat application."""

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

    #prompt-input {
        dock: bottom;
    }
    """

    def __init__(
        self,
        provider: LLMProvider,
        history: ConversationHistory,
        *,
        provider_id: str,
        model: str,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        super().__init__()
        self.provider = provider
        self.history = history
        self.provider_id = provider_id
        self.model = model
        self.tool_registry = tool_registry
        self.active_response = ""

    def compose(self) -> ComposeResult:
        yield RichLog(id="chat-log", wrap=True, markup=False)
        yield Static(id="status")
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
        if self.active_response:
            log.write(f"Assistant: {self.active_response}")

    @on(Input.Submitted, "#prompt-input")
    def submit_prompt(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        prompt_input = self.query_one("#prompt-input", Input)
        prompt_input.value = ""
        if not prompt or prompt_input.disabled:
            return

        self.history.add_user(prompt)
        self.active_response = ""
        self._render_transcript()
        prompt_input.disabled = True
        self._set_status("生成中")
        self.stream_response()

    @work(exclusive=True, exit_on_error=False)
    async def stream_response(self) -> None:
        prompt_input = self.query_one("#prompt-input", Input)
        try:
            if self.tool_registry is None:
                tool_calls = await self._stream_round(None)
                if tool_calls:
                    raise ProviderError("收到工具调用，但工具注册中心未启用")
                self.history.add_assistant(self.active_response)
                self.active_response = ""
                self._render_transcript()
            else:
                await self._run_agent_loop()
            self._set_status("就绪")
        except ProviderError as exc:
            self.active_response = ""
            self._render_transcript()
            self._set_status(f"错误：{exc}")
        finally:
            prompt_input.disabled = False
            prompt_input.focus()

    async def _stream_round(
        self,
        tools: list[dict[str, object]] | None,
    ) -> tuple[ToolCall, ...]:
        self.active_response = ""
        if tools is None:
            stream = self.provider.stream_chat(self.history.snapshot())
        else:
            stream = self.provider.stream_chat(
                self.history.snapshot(),
                tools=tools,
            )

        tool_calls: list[ToolCall] = []
        async for part in stream:
            if isinstance(part, ToolCall):
                tool_calls.append(part)
            else:
                self.active_response += part
                self._render_transcript()
        return tuple(tool_calls)

    async def _run_agent_loop(self) -> None:
        if self.tool_registry is None:
            raise ProviderError("工具注册中心未启用")

        api_tools = self.tool_registry.api_tools(self.provider.protocol)
        executed_count = 0

        while True:
            tools_enabled = executed_count < MAX_TOOL_CALLS_PER_TURN
            tool_calls = await self._stream_round(
                api_tools if tools_enabled else None
            )
            if not tool_calls:
                self.history.add_assistant(self.active_response)
                self.active_response = ""
                self._render_transcript()
                return

            self.history.add_assistant_tool_calls(
                self.active_response,
                tool_calls,
            )
            self.active_response = ""
            self._render_transcript()

            for tool_call in tool_calls:
                if executed_count >= MAX_TOOL_CALLS_PER_TURN:
                    result = ToolResult(
                        tool_name=tool_call.name,
                        success=False,
                        error_code="tool_limit_reached",
                        error_message=(
                            "本轮工具调用已达到上限 "
                            f"{MAX_TOOL_CALLS_PER_TURN}"
                        ),
                    )
                else:
                    executed_count += 1
                    self._set_status(
                        f"执行工具 {executed_count}/"
                        f"{MAX_TOOL_CALLS_PER_TURN}：{tool_call.name}"
                    )
                    result = await self.tool_registry.execute(
                        tool_call.name,
                        tool_call.arguments_json,
                    )
                self.history.add_tool_result(tool_call.call_id, result)
                self._render_transcript()

            if not tools_enabled:
                raise ProviderError("达到工具调用上限后模型仍返回了工具调用")
            if executed_count >= MAX_TOOL_CALLS_PER_TURN:
                self._set_status("工具调用已达上限，生成最终总结")
            else:
                self._set_status(
                    f"继续生成（已执行 {executed_count}/"
                    f"{MAX_TOOL_CALLS_PER_TURN} 个工具）"
                )
