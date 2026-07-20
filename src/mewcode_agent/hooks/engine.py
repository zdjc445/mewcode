"""Deterministic Hook dispatch, isolation, and background task draining."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mewcode_agent.hooks.actions import (
    HookActionError,
    HookActionRunner,
    PreparedHookAction,
)
from mewcode_agent.hooks.matching import rule_matches
from mewcode_agent.hooks.models import (
    HOOK_EVENT_NAMES,
    HookCloseResult,
    HookConfiguration,
    HookDiagnostic,
    HookDispatchResult,
    HookEventName,
    HookRule,
    action_type,
    validate_context_path,
)
from mewcode_agent.hooks.templates import HookTemplateError, render_template


HookDiagnosticHandler = Callable[[HookDiagnostic], None]
SessionIdProvider = Callable[[], str | None]
_DEFAULT_SESSION = object()


class HookEngine:
    def __init__(
        self,
        configuration: HookConfiguration,
        action_runner: HookActionRunner,
        *,
        project_root: Path,
        session_id_provider: SessionIdProvider | None = None,
        diagnostic_handler: HookDiagnosticHandler | None = None,
    ) -> None:
        if not isinstance(configuration, HookConfiguration):
            raise ValueError("configuration 类型无效")
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise ValueError("project_root 必须是绝对 Path")
        self._rules_by_event: dict[HookEventName, tuple[HookRule, ...]] = {
            event: tuple(
                rule for rule in configuration.rules if rule.event == event
            )
            for event in HOOK_EVENT_NAMES
        }
        self._action_runner = action_runner
        self._project_root = project_root
        self._session_id_provider = session_id_provider
        self._diagnostic_handler = diagnostic_handler
        self._once_consumed: set[str] = set()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._event_sequence = 0
        self._closing = False
        self._closed = False
        self._shutdown_dispatched = False
        self._accept_background = True
        self._close_lock = asyncio.Lock()
        self._close_result: HookCloseResult | None = None

    async def dispatch(
        self,
        event: HookEventName,
        values: Mapping[str, Any] | None = None,
        *,
        session_id: str | None | object = _DEFAULT_SESSION,
    ) -> HookDispatchResult:
        if self._closing or self._closed:
            return HookDispatchResult()
        return await self._dispatch(
            event,
            values or {},
            session_id=session_id,
        )

    async def _dispatch(
        self,
        event: HookEventName,
        values: Mapping[str, Any],
        *,
        allow_closing: bool = False,
        session_id: str | None | object = _DEFAULT_SESSION,
    ) -> HookDispatchResult:
        if event not in HOOK_EVENT_NAMES:
            raise ValueError("Hook event 无效")
        if (self._closing or self._closed) and not allow_closing:
            return HookDispatchResult()
        self._event_sequence += 1
        sequence = self._event_sequence
        try:
            context = self._build_context(
                event,
                sequence,
                values,
                session_id=session_id,
            )
        except (TypeError, ValueError, RuntimeError):
            self._diagnose(
                HookDiagnostic(
                    None,
                    None,
                    event,
                    None,
                    "hook_context_invalid",
                    "Hook 事件上下文无效",
                )
            )
            return HookDispatchResult()

        for rule in self._rules_by_event[event]:
            if rule.once and rule.rule_id in self._once_consumed:
                continue
            try:
                matched = rule_matches(rule.matchers, context)
            except Exception:
                self._rule_diagnostic(
                    rule,
                    "hook_match_failed",
                    "Hook 条件匹配失败",
                )
                continue
            if not matched:
                continue

            prepared: PreparedHookAction | None = None
            try:
                prepared = self._action_runner.prepare(
                    rule.action,
                    dict(context),
                )
            except HookTemplateError:
                self._rule_diagnostic(
                    rule,
                    "hook_template_field_missing",
                    "Hook 模板字段不存在",
                )
            except HookActionError as exc:
                self._rule_diagnostic(rule, exc.code, exc.message)
            except (TypeError, ValueError):
                self._rule_diagnostic(
                    rule,
                    "hook_action_failed",
                    "Hook 动作准备失败",
                )

            block_reason: str | None = None
            if rule.interception is not None:
                try:
                    block_reason = render_template(
                        rule.interception.reason,
                        context,
                    )
                except HookTemplateError:
                    self._rule_diagnostic(
                        rule,
                        "hook_template_field_missing",
                        "Hook 拦截原因模板字段不存在",
                    )
                    block_reason = "工具调用被 Hook 规则拒绝"

            if prepared is not None:
                if rule.once:
                    self._once_consumed.add(rule.rule_id)
                if rule.run_async:
                    if self._accept_background:
                        task = asyncio.create_task(
                            self._execute_rule(
                                rule,
                                prepared,
                                sequence=sequence,
                                background=True,
                            )
                        )
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)
                else:
                    await self._execute_rule(
                        rule,
                        prepared,
                        sequence=sequence,
                        background=False,
                    )

            if block_reason is not None:
                return HookDispatchResult(True, block_reason)
        return HookDispatchResult()

    def _build_context(
        self,
        event: HookEventName,
        sequence: int,
        values: Mapping[str, Any],
        *,
        session_id: str | None | object,
    ) -> dict[str, Any]:
        if not isinstance(values, Mapping):
            raise TypeError("values 必须是 Mapping")
        context: dict[str, Any] = {
            "event.name": event,
            "event.sequence": sequence,
            "project.root": str(self._project_root),
        }
        resolved_session_id = session_id
        if (
            resolved_session_id is _DEFAULT_SESSION
            and self._session_id_provider is not None
        ):
            resolved_session_id = self._session_id_provider()
        if resolved_session_id is not _DEFAULT_SESSION:
            if resolved_session_id is not None:
                if (
                    not isinstance(resolved_session_id, str)
                    or not resolved_session_id
                ):
                    raise ValueError("session id 无效")
                context["session.id"] = resolved_session_id
        for path, value in values.items():
            if not isinstance(path, str) or not validate_context_path(path):
                raise ValueError("Hook context path 无效")
            if path in context:
                raise ValueError("Hook context 不能覆盖公共字段")
            context[path] = value
        return context

    async def _execute_rule(
        self,
        rule: HookRule,
        prepared: PreparedHookAction,
        *,
        sequence: int,
        background: bool,
    ) -> None:
        try:
            async with asyncio.timeout(rule.timeout_seconds):
                await self._action_runner.execute(
                    prepared,
                    event_sequence=sequence,
                    rule_id=rule.rule_id,
                )
        except TimeoutError:
            self._rule_diagnostic(
                rule,
                "hook_action_timeout",
                "Hook 动作执行超时",
            )
        except HookActionError as exc:
            self._rule_diagnostic(rule, exc.code, exc.message)
        except asyncio.CancelledError:
            self._rule_diagnostic(
                rule,
                "hook_background_cancelled",
                "Hook 后台动作被取消",
            )
            if not background:
                raise
        except Exception:
            self._rule_diagnostic(
                rule,
                "hook_action_failed",
                "Hook 动作执行失败",
            )

    def _rule_diagnostic(
        self,
        rule: HookRule,
        code: str,
        message: str,
    ) -> None:
        self._diagnose(
            HookDiagnostic(
                rule.source,
                rule.rule_id,
                rule.event,
                action_type(rule.action),
                code,
                message,
            )
        )

    def _diagnose(self, diagnostic: HookDiagnostic) -> None:
        if self._diagnostic_handler is None:
            return
        try:
            self._diagnostic_handler(diagnostic)
        except Exception:
            return

    async def close(self) -> HookCloseResult:
        async with self._close_lock:
            if self._close_result is not None:
                return self._close_result
            self._closing = True
            if not self._shutdown_dispatched:
                self._shutdown_dispatched = True
                await self._dispatch(
                    "system.shutdown",
                    {},
                    allow_closing=True,
                )
            self._accept_background = False
            waited = len(self._background_tasks)
            while self._background_tasks:
                await asyncio.gather(
                    *tuple(self._background_tasks),
                    return_exceptions=True,
                )
            pending = await self._action_runner.close()
            self._closed = True
            self._close_result = HookCloseResult(waited, pending)
            return self._close_result

    async def flush_pending_prompts(self) -> None:
        if self._closing or self._closed:
            return
        try:
            failed_rule_ids = (
                await self._action_runner.flush_pending_prompts()
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._diagnose(
                HookDiagnostic(
                    None,
                    None,
                    "message.before_send",
                    "prompt",
                    "hook_prompt_failed",
                    "Hook pending Prompt 注入失败",
                )
            )
            return
        if not failed_rule_ids:
            return
        rules = {
            rule.rule_id: rule
            for event_rules in self._rules_by_event.values()
            for rule in event_rules
        }
        for rule_id in failed_rule_ids:
            rule = rules.get(rule_id)
            if rule is None:
                self._diagnose(
                    HookDiagnostic(
                        None,
                        rule_id,
                        "message.before_send",
                        "prompt",
                        "hook_prompt_failed",
                        "Hook pending Prompt 注入失败",
                    )
                )
            else:
                self._rule_diagnostic(
                    rule,
                    "hook_prompt_failed",
                    "Hook pending Prompt 注入失败",
                )

    def reset_session_prompts(self) -> int:
        startup_prompt_rules = frozenset(
            rule.rule_id
            for rule in self._rules_by_event["system.startup"]
            if action_type(rule.action) == "prompt"
        )
        return self._action_runner.reset_prompt_session(
            preserve_rule_ids=startup_prompt_rules
        )

    @property
    def background_task_count(self) -> int:
        return len(self._background_tasks)
