"""Adapters from Agent, Prompt, session, and tool lifecycles to Hook events."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
from typing import Any

from mewcode_agent.hooks.engine import HookEngine
from mewcode_agent.hooks.models import validate_context_path
from mewcode_agent.models import ToolCall
from mewcode_agent.prompting.models import RuntimeInstruction
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.tools.base import ToolResult


@dataclass(frozen=True, slots=True)
class _PendingPrompt:
    content: str
    event_sequence: int
    rule_id: str


@dataclass(slots=True)
class _PromptTarget:
    runtime: PromptRuntime
    history_length_provider: Callable[[], int]
    pending: list[_PendingPrompt]


class PromptHookBridge:
    """Inject request controls now or queue them for the next request."""

    def __init__(
        self,
        prompt_runtime: PromptRuntime,
        *,
        history_length_provider: Callable[[], int],
    ) -> None:
        self._main_target = _PromptTarget(
            prompt_runtime,
            history_length_provider,
            [],
        )
        self._bound_target: ContextVar[_PromptTarget | None] = ContextVar(
            f"mewcode_hook_prompt_target_{id(self)}",
            default=None,
        )

    def _target(self) -> _PromptTarget:
        return self._bound_target.get() or self._main_target

    @contextmanager
    def bind_runtime(
        self,
        prompt_runtime: PromptRuntime,
        *,
        history_length_provider: Callable[[], int],
    ) -> Iterator[None]:
        """Route Hook prompt actions to one isolated Agent runtime."""

        target = _PromptTarget(prompt_runtime, history_length_provider, [])
        token = self._bound_target.set(target)
        try:
            yield
        finally:
            target.pending.clear()
            self._bound_target.reset(token)

    async def inject(
        self,
        content: str,
        *,
        event_sequence: int,
        rule_id: str,
    ) -> None:
        target = self._target()
        item = _PendingPrompt(content, event_sequence, rule_id)
        if target.runtime.active_request_sequence is None:
            target.pending.append(item)
            return
        self._inject_now(target, item)

    async def flush(self) -> tuple[str, ...]:
        target = self._target()
        if target.runtime.active_request_sequence is None:
            return ()
        pending = tuple(target.pending)
        target.pending.clear()
        failed: list[str] = []
        for item in pending:
            try:
                self._inject_now(target, item)
            except (ValueError, RuntimeError):
                failed.append(item.rule_id)
        return tuple(failed)

    @staticmethod
    def _inject_now(target: _PromptTarget, item: _PendingPrompt) -> None:
        target.runtime.inject(
            RuntimeInstruction(
                (
                    f"hook.prompt.event_{item.event_sequence}."
                    f"rule_{item.rule_id}"
                ),
                "instruction",
                "request",
                item.content,
                "hook",
            ),
            history_length=target.history_length_provider(),
        )

    def discard_pending(self) -> int:
        target = self._target()
        count = len(target.pending)
        target.pending.clear()
        return count

    def reset_session(
        self,
        *,
        preserve_rule_ids: frozenset[str],
    ) -> int:
        target = self._target()
        retained = [
            item
            for item in target.pending
            if item.rule_id in preserve_rule_ids
        ]
        discarded = len(target.pending) - len(retained)
        target.pending = retained
        return discarded

    @property
    def pending_count(self) -> int:
        return len(self._target().pending)


class HookToolExecutionInterceptor:
    def __init__(self, engine: HookEngine) -> None:
        self._engine = engine

    async def before_execute(
        self,
        tool_call: ToolCall,
        *,
        plan_only: bool,
        current_request_authorized: bool,
    ) -> ToolResult | None:
        del plan_only, current_request_authorized
        dispatch = await self._engine.dispatch(
            "tool.before_execute",
            _tool_context(tool_call),
        )
        if not dispatch.blocked:
            return None
        return ToolResult(
            tool_name=tool_call.name,
            success=False,
            error_code="tool_blocked_by_hook",
            error_message=dispatch.block_reason,
        )

    async def after_execute(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        values = _tool_context(tool_call)
        values["tool.result.success"] = result.success
        if result.data is not None:
            values["tool.result.data"] = result.data
        if result.error_code is not None:
            values["tool.result.error_code"] = result.error_code
        if result.error_message is not None:
            values["tool.result.error_message"] = result.error_message
        await self._engine.dispatch("tool.after_execute", values)
        return result


def _tool_context(tool_call: ToolCall) -> dict[str, Any]:
    values: dict[str, Any] = {
        "tool.call_id": tool_call.call_id,
        "tool.name": tool_call.name,
        "tool.arguments_json": tool_call.arguments_json,
    }
    try:
        arguments = json.loads(tool_call.arguments_json)
    except json.JSONDecodeError:
        return values
    if not isinstance(arguments, dict):
        return values
    for key, value in arguments.items():
        if not isinstance(key, str):
            continue
        path = f"tool.arguments.{key}"
        if validate_context_path(path):
            values[path] = value
    path_value = arguments.get("path")
    if isinstance(path_value, str):
        values["file.path"] = path_value
    return values


class HookLifecycle:
    """Emit exactly-once startup and active-session lifecycle events."""

    def __init__(
        self,
        engine: HookEngine,
        *,
        active_session_id: Callable[[], str],
    ) -> None:
        self._engine = engine
        self._active_session_id = active_session_id
        self._started = False
        self._active_emitted: str | None = None
        self._active_restored = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        session_id = self._active_session_id()
        await self._engine.dispatch("system.startup", session_id=session_id)
        await self._engine.dispatch(
            "session.started",
            {"session.restored": False},
            session_id=session_id,
        )
        self._active_emitted = session_id
        self._active_restored = False

    async def session_switched(
        self,
        previous_session_id: str,
        *,
        restored: bool,
    ) -> None:
        current_session_id = self._active_session_id()
        if self._active_emitted == previous_session_id:
            await self._engine.dispatch(
                "session.ended",
                {"session.restored": self._active_restored},
                session_id=previous_session_id,
            )
        self._engine.reset_session_prompts()
        await self._engine.dispatch(
            "session.started",
            {"session.restored": restored},
            session_id=current_session_id,
        )
        self._active_emitted = current_session_id
        self._active_restored = restored

    async def end_active_session(self) -> None:
        if self._active_emitted is None:
            return
        session_id = self._active_emitted
        self._active_emitted = None
        await self._engine.dispatch(
            "session.ended",
            {"session.restored": self._active_restored},
            session_id=session_id,
        )
        self._active_restored = False
