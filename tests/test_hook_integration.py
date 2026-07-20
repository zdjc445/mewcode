from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from mewcode_agent.agent.context import AgentRunContext
from mewcode_agent.agent.events import ToolResultEvent
from mewcode_agent.agent.tool_scheduler import ToolScheduler
from mewcode_agent.hooks import (
    HookDispatchResult,
    HookLifecycle,
    HookToolExecutionInterceptor,
    PromptHookBridge,
)
from mewcode_agent.models import ToolCall
from mewcode_agent.prompting.environment import (
    GitEnvironment,
    RequestEnvironment,
    SessionEnvironment,
)
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.tools import Tool, ToolRegistry, ToolResult


class FixedRequestEnvironmentCollector:
    async def collect(self) -> RequestEnvironment:
        return RequestEnvironment(
            "2026-07-21T12:00:00+08:00",
            GitEnvironment("repository", "master", "", None),
        )


def make_runtime() -> PromptRuntime:
    return PromptRuntime(
        SessionEnvironment(
            "Windows",
            "powershell.exe",
            "D:\\workspace",
            "China Standard Time",
            "+08:00",
        ),
        FixedRequestEnvironmentCollector(),
    )


async def test_prompt_bridge_queues_then_flushes_request_controls() -> None:
    runtime = make_runtime()
    history_length = 0
    bridge = PromptHookBridge(
        runtime,
        history_length_provider=lambda: history_length,
    )

    await bridge.inject(
        "startup instruction",
        event_sequence=1,
        rule_id="startup",
    )
    assert bridge.pending_count == 1
    await runtime.begin_request(history_length=0, mode="executing")
    failed = await bridge.flush()

    assert failed == ()
    assert bridge.pending_count == 0
    control = next(
        item
        for item in runtime.timeline()
        if item.instruction_id == "hook.prompt.event_1.rule_startup"
    )
    assert control.scope == "request"
    assert control.content == "startup instruction"


async def test_prompt_bridge_session_reset_only_preserves_startup_rules() -> None:
    runtime = make_runtime()
    bridge = PromptHookBridge(runtime, history_length_provider=lambda: 0)
    await bridge.inject(
        "startup",
        event_sequence=1,
        rule_id="startup_rule",
    )
    await bridge.inject(
        "old session",
        event_sequence=2,
        rule_id="session_rule",
    )

    discarded = bridge.reset_session(
        preserve_rule_ids=frozenset({"startup_rule"})
    )

    assert discarded == 1
    assert bridge.pending_count == 1


class FakeHookEngine:
    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.events: list[tuple[str, dict[str, Any], str | None]] = []
        self.prompt_resets = 0

    async def dispatch(
        self,
        event: str,
        values: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> HookDispatchResult:
        self.events.append((event, dict(values or {}), session_id))
        if event == "tool.before_execute" and self.block:
            return HookDispatchResult(True, "blocked by test")
        return HookDispatchResult()

    def reset_session_prompts(self) -> int:
        self.prompt_resets += 1
        return 0


async def test_tool_interceptor_builds_exact_context_and_preserves_result() -> None:
    engine = FakeHookEngine()
    interceptor = HookToolExecutionInterceptor(engine)  # type: ignore[arg-type]
    call = ToolCall(
        "call-1",
        "write_file",
        '{"path":"src/a.py","valid_key":3,"Bad-Key":4}',
    )

    before = await interceptor.before_execute(
        call,
        plan_only=False,
        current_request_authorized=False,
    )
    result = ToolResult("write_file", True, {"written": True})
    returned = await interceptor.after_execute(call, result)

    assert before is None
    assert returned is result
    before_values = engine.events[0][1]
    assert before_values["file.path"] == "src/a.py"
    assert before_values["tool.arguments.path"] == "src/a.py"
    assert before_values["tool.arguments.valid_key"] == 3
    assert "tool.arguments.Bad-Key" not in before_values
    assert engine.events[1][1]["tool.result.data"] == {"written": True}


class RecordingTool(Tool):
    name = "write_file"
    description = "test"
    category = "write"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, arguments: dict[str, Any]) -> Any:
        self.executions += 1
        return arguments


async def _events(
    scheduler: ToolScheduler,
    call: ToolCall,
) -> AsyncIterator[object]:
    context = AgentRunContext()
    context.begin_run()
    async for event in scheduler.run(
        (call,),
        plan_only=False,
        current_request_authorized=False,
        context=context,
    ):
        yield event
    context.finish_run()


async def test_tool_scheduler_hook_denial_skips_handler_and_emits_after() -> None:
    engine = FakeHookEngine(block=True)
    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    scheduler = ToolScheduler(
        registry,
        interceptor=HookToolExecutionInterceptor(  # type: ignore[arg-type]
            engine
        ),
    )

    events = [
        item
        async for item in _events(
            scheduler,
            ToolCall("call", "write_file", '{"path":"a.py"}'),
        )
    ]

    result = next(
        item.result for item in events if isinstance(item, ToolResultEvent)
    )
    assert tool.executions == 0
    assert result.error_code == "tool_blocked_by_hook"
    assert [item[0] for item in engine.events] == [
        "tool.before_execute",
        "tool.after_execute",
    ]


async def test_lifecycle_uses_previous_session_id_after_successful_switch() -> None:
    active = "a" * 32
    engine = FakeHookEngine()
    lifecycle = HookLifecycle(
        engine,  # type: ignore[arg-type]
        active_session_id=lambda: active,
    )

    await lifecycle.start()
    previous = active
    active = "b" * 32
    await lifecycle.session_switched(previous, restored=True)
    await lifecycle.end_active_session()

    assert [(event, session_id) for event, _, session_id in engine.events] == [
        ("system.startup", "a" * 32),
        ("session.started", "a" * 32),
        ("session.ended", "a" * 32),
        ("session.started", "b" * 32),
        ("session.ended", "b" * 32),
    ]
    assert engine.events[3][1]["session.restored"] is True
    assert engine.prompt_resets == 1
