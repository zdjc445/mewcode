from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

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
    UserMessageEvent,
)
from mewcode_agent.agent.context import AgentRunCancelled
from mewcode_agent.app import ChatApp
from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage


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


@pytest.mark.asyncio
async def test_app_consumes_agent_events_and_restores_input() -> None:
    loop = GatedAgentLoop()
    history = ConversationHistory()
    app = make_app(loop, history)

    async with app.run_test() as pilot:
        prompt_input = app.query_one("#prompt-input", Input)
        plan_switch = app.query_one("#plan-only-switch", Switch)
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
@pytest.mark.parametrize(
    ("button_id", "expected"),
    [("#allow-once", "allow_once"), ("#reject-tool", "reject")],
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
