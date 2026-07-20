"""Declarative lifecycle Hook configuration and execution."""

from mewcode_agent.hooks.actions import (
    HookActionError,
    HookActionRunner,
    HookPromptSink,
)
from mewcode_agent.hooks.engine import HookEngine
from mewcode_agent.hooks.loader import load_hook_configuration
from mewcode_agent.hooks.matching import matcher_matches, rule_matches
from mewcode_agent.hooks.models import (
    HOOK_EVENT_NAMES,
    HookAction,
    HookActionType,
    HookCloseResult,
    HookConfigError,
    HookConfiguration,
    HookDiagnostic,
    HookDispatchResult,
    HookEventName,
    HookInterception,
    HookRule,
    HookSource,
    HookValueMatcher,
    HttpHookAction,
    PromptHookAction,
    ShellHookAction,
    SubagentHookAction,
)
from mewcode_agent.hooks.templates import (
    HookTemplateError,
    render_template,
    validate_template,
)

__all__ = [
    "HOOK_EVENT_NAMES",
    "HookAction",
    "HookActionError",
    "HookActionRunner",
    "HookActionType",
    "HookCloseResult",
    "HookConfigError",
    "HookConfiguration",
    "HookDiagnostic",
    "HookDispatchResult",
    "HookEngine",
    "HookEventName",
    "HookInterception",
    "HookPromptSink",
    "HookRule",
    "HookSource",
    "HookTemplateError",
    "HookValueMatcher",
    "HttpHookAction",
    "PromptHookAction",
    "ShellHookAction",
    "SubagentHookAction",
    "load_hook_configuration",
    "matcher_matches",
    "render_template",
    "rule_matches",
    "validate_template",
]
