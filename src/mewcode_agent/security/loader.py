"""Strict two-layer loading for security policy rules."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from mewcode_agent.security._yaml import (
    SecurityConfigError,
    read_yaml_mapping,
    require_exact_keys,
)
from mewcode_agent.security.models import (
    ArgumentMatcher,
    PermissionMode,
    RuleScope,
    SecurityConfiguration,
    SecurityRule,
)


def _parse_matchers(
    raw: Any,
    *,
    location: str,
) -> tuple[ArgumentMatcher, ...]:
    if not isinstance(raw, Mapping):
        raise SecurityConfigError(f"{location} 必须是映射")
    matchers: list[ArgumentMatcher] = []
    for argument, matcher_raw in raw.items():
        if not isinstance(argument, str):
            raise SecurityConfigError(f"{location} 参数名必须是字符串")
        if not isinstance(matcher_raw, Mapping):
            raise SecurityConfigError(f"{location}.{argument} 必须是映射")
        matcher_data = cast(Mapping[str, Any], matcher_raw)
        require_exact_keys(
            matcher_data,
            required={"kind", "pattern"},
            optional=set(),
            location=f"{location}.{argument}",
        )
        kind = matcher_data["kind"]
        pattern = matcher_data["pattern"]
        try:
            matchers.append(ArgumentMatcher(argument, kind, pattern))
        except ValueError as exc:
            raise SecurityConfigError(
                f"{location}.{argument} 无效: {exc}"
            ) from exc
    return tuple(matchers)


def _parse_rule(
    raw: Any,
    *,
    scope: RuleScope,
    location: str,
) -> SecurityRule:
    if not isinstance(raw, Mapping):
        raise SecurityConfigError(f"{location} 必须是映射")
    data = cast(Mapping[str, Any], raw)
    require_exact_keys(
        data,
        required={"id", "action", "tool", "priority", "match"},
        optional=set(),
        location=location,
    )
    try:
        return SecurityRule(
            rule_id=data["id"],
            scope=scope,
            priority=data["priority"],
            action=data["action"],
            tool_name=data["tool"],
            matchers=_parse_matchers(
                data["match"],
                location=f"{location}.match",
            ),
        )
    except ValueError as exc:
        raise SecurityConfigError(f"{location} 无效: {exc}") from exc


def _read_layer(
    path: Path,
    *,
    scope: RuleScope,
    allow_mode: bool,
) -> tuple[PermissionMode | None, tuple[SecurityRule, ...]]:
    label = "用户全局" if scope == "user" else "项目"
    data = read_yaml_mapping(path, label=label)
    if data is None:
        return None, ()
    require_exact_keys(
        data,
        required={"version", "rules"},
        optional={"mode"} if allow_mode else set(),
        location=f"{label}安全配置",
    )
    if type(data["version"]) is not int or data["version"] != 1:
        raise SecurityConfigError(f"{label}安全配置 version 必须为整数 1")
    raw_rules = data["rules"]
    if not isinstance(raw_rules, list):
        raise SecurityConfigError(f"{label}安全配置 rules 必须是列表")
    rules = tuple(
        _parse_rule(
            raw,
            scope=scope,
            location=f"{label}安全配置.rules[{index}]",
        )
        for index, raw in enumerate(raw_rules)
    )
    identifiers = [rule.rule_id for rule in rules]
    if len(identifiers) != len(set(identifiers)):
        raise SecurityConfigError(f"{label}安全配置包含重复规则 id")
    raw_mode = data.get("mode")
    if raw_mode is None:
        return None, rules
    if raw_mode not in ("strict", "default", "permissive"):
        raise SecurityConfigError(
            f"{label}安全配置 mode 必须为 strict、default 或 permissive"
        )
    return cast(PermissionMode, raw_mode), rules


def load_security_configuration(
    *,
    user_path: Path,
    project_path: Path,
    permanent_rules: tuple[SecurityRule, ...] = (),
) -> SecurityConfiguration:
    user_mode, user_rules = _read_layer(
        user_path,
        scope="user",
        allow_mode=True,
    )
    _, project_rules = _read_layer(
        project_path,
        scope="project",
        allow_mode=False,
    )
    return SecurityConfiguration(
        mode=user_mode or "default",
        user_rules=user_rules,
        project_rules=project_rules,
        permanent_rules=permanent_rules,
    )
