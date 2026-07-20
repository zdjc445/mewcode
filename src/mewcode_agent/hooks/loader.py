"""Strict two-layer loading for declarative Hook rules."""

from __future__ import annotations

from collections.abc import Mapping
import re
from pathlib import Path
from typing import Any, cast

from mewcode_agent.hooks._yaml import read_hook_yaml, require_exact_keys
from mewcode_agent.hooks.models import (
    HOOK_EVENT_NAMES,
    HookAction,
    HookConfigError,
    HookConfiguration,
    HookInterception,
    HookRule,
    HookSource,
    HookValueMatcher,
    HttpHookAction,
    PromptHookAction,
    ShellHookAction,
    SubagentHookAction,
)
from mewcode_agent.hooks.templates import validate_template


def _mapping(raw: Any, *, location: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise HookConfigError(f"{location} 必须是映射")
    if any(not isinstance(key, str) for key in raw):
        raise HookConfigError(f"{location} 字段名必须是字符串")
    return cast(Mapping[str, Any], raw)


def _validate_action_template(value: str, *, location: str) -> str:
    try:
        validate_template(value)
    except ValueError as exc:
        raise HookConfigError(f"{location} 无效: {exc}") from exc
    return value


def _parse_matcher(
    raw: Any,
    *,
    location: str,
    depth: int = 1,
) -> HookValueMatcher:
    if depth > 8:
        raise HookConfigError(f"{location} not matcher 嵌套超过 8 层")
    data = _mapping(raw, location=location)
    require_exact_keys(
        data,
        required={"kind", "pattern"},
        location=location,
    )
    kind = data["kind"]
    pattern = data["pattern"]
    if kind == "not":
        pattern = _parse_matcher(
            pattern,
            location=f"{location}.pattern",
            depth=depth + 1,
        )
    elif kind == "regex":
        if not isinstance(pattern, str) or not pattern:
            raise HookConfigError(
                f"{location}.pattern 必须是非空字符串"
            )
        if len(pattern) > 4096:
            raise HookConfigError(
                f"{location}.pattern 长度不能超过 4096"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise HookConfigError(
                f"{location}.pattern 不是有效正则"
            ) from exc
    try:
        return HookValueMatcher(kind, pattern)
    except ValueError as exc:
        raise HookConfigError(f"{location} 无效: {exc}") from exc


def _parse_matchers(
    raw: Any,
    *,
    location: str,
) -> dict[str, HookValueMatcher]:
    data = _mapping(raw, location=location)
    return {
        path: _parse_matcher(
            matcher,
            location=f"{location}.{path}",
        )
        for path, matcher in data.items()
    }


def _non_empty_string(value: Any, *, location: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise HookConfigError(f"{location} 必须是非空字符串")
    return value


def _parse_action(raw: Any, *, location: str) -> HookAction:
    data = _mapping(raw, location=location)
    action_type = data.get("type")
    if action_type == "shell":
        require_exact_keys(
            data,
            required={"type", "command", "cwd"},
            location=location,
        )
        command = _non_empty_string(
            data["command"],
            location=f"{location}.command",
        )
        _validate_action_template(command, location=f"{location}.command")
        if data["cwd"] != "project":
            raise HookConfigError(f"{location}.cwd 必须是 project")
        return ShellHookAction(command)
    if action_type == "prompt":
        require_exact_keys(
            data,
            required={"type", "content"},
            location=location,
        )
        content = _non_empty_string(
            data["content"],
            location=f"{location}.content",
        )
        _validate_action_template(content, location=f"{location}.content")
        return PromptHookAction(content)
    if action_type == "http":
        require_exact_keys(
            data,
            required={"type", "method", "url", "headers", "body"},
            location=location,
        )
        method = data["method"]
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise HookConfigError(f"{location}.method 无效")
        url = _non_empty_string(data["url"], location=f"{location}.url")
        _validate_action_template(url, location=f"{location}.url")
        raw_headers = _mapping(
            data["headers"],
            location=f"{location}.headers",
        )
        headers: dict[str, str] = {}
        for name, value in raw_headers.items():
            if not name or "\n" in name or "\r" in name or "${" in name:
                raise HookConfigError(
                    f"{location}.headers header name 无效"
                )
            if not isinstance(value, str) or "\n" in value or "\r" in value:
                raise HookConfigError(
                    f"{location}.headers.{name} 必须是单行字符串"
                )
            headers[name] = _validate_action_template(
                value,
                location=f"{location}.headers.{name}",
            )
        body = data["body"]
        if not isinstance(body, str):
            raise HookConfigError(f"{location}.body 必须是字符串")
        _validate_action_template(body, location=f"{location}.body")
        return HttpHookAction(method, url, headers, body)
    if action_type == "subagent":
        require_exact_keys(
            data,
            required={"type", "task", "context"},
            location=location,
        )
        task = _non_empty_string(data["task"], location=f"{location}.task")
        _validate_action_template(task, location=f"{location}.task")
        context = data["context"]
        if context not in ("summary", "recent", "none"):
            raise HookConfigError(f"{location}.context 无效")
        return SubagentHookAction(task, context)
    raise HookConfigError(f"{location}.type 无效")


def _parse_interception(
    raw: Any,
    *,
    location: str,
) -> HookInterception | None:
    if raw is None:
        return None
    data = _mapping(raw, location=location)
    require_exact_keys(
        data,
        required={"deny", "reason"},
        location=location,
    )
    if data["deny"] is not True:
        raise HookConfigError(f"{location}.deny 必须是 true")
    reason = _non_empty_string(
        data["reason"],
        location=f"{location}.reason",
    )
    _validate_action_template(reason, location=f"{location}.reason")
    return HookInterception(True, reason)


def _parse_rule(
    raw: Any,
    *,
    source: HookSource,
    location: str,
) -> HookRule:
    data = _mapping(raw, location=location)
    require_exact_keys(
        data,
        required={
            "id",
            "event",
            "once",
            "async",
            "timeout_seconds",
            "match",
            "action",
            "intercept",
        },
        location=location,
    )
    if data["event"] not in HOOK_EVENT_NAMES:
        raise HookConfigError(f"{location}.event 无效")
    try:
        return HookRule(
            rule_id=data["id"],
            source=source,
            event=data["event"],
            once=data["once"],
            run_async=data["async"],
            timeout_seconds=data["timeout_seconds"],
            matchers=_parse_matchers(
                data["match"],
                location=f"{location}.match",
            ),
            action=_parse_action(
                data["action"],
                location=f"{location}.action",
            ),
            interception=_parse_interception(
                data["intercept"],
                location=f"{location}.intercept",
            ),
        )
    except ValueError as exc:
        raise HookConfigError(f"{location} 无效: {exc}") from exc


def _read_layer(
    path: Path,
    *,
    source: HookSource,
) -> tuple[HookRule, ...]:
    label = "用户" if source == "user" else "项目"
    data = read_hook_yaml(path, label=label)
    if data is None:
        return ()
    require_exact_keys(
        data,
        required={"version", "rules"},
        location=f"{label} Hook 配置",
    )
    if type(data["version"]) is not int or data["version"] != 1:
        raise HookConfigError(f"{label} Hook 配置.version 必须是整数 1")
    raw_rules = data["rules"]
    if not isinstance(raw_rules, list):
        raise HookConfigError(f"{label} Hook 配置.rules 必须是列表")
    rules = tuple(
        _parse_rule(
            raw,
            source=source,
            location=f"{label} Hook 配置.rules[{index}]",
        )
        for index, raw in enumerate(raw_rules)
    )
    identifiers = [rule.rule_id for rule in rules]
    if len(identifiers) != len(set(identifiers)):
        raise HookConfigError(f"{label} Hook 配置包含重复规则 id")
    return rules


def load_hook_configuration(
    *,
    user_path: Path,
    project_path: Path,
) -> HookConfiguration:
    user_rules = _read_layer(user_path, source="user")
    project_rules = _read_layer(project_path, source="project")
    project_ids = {rule.rule_id for rule in project_rules}
    return HookConfiguration(
        (*project_rules, *(rule for rule in user_rules if rule.rule_id not in project_ids))
    )
