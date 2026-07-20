"""Context matching for declarative Hook rules."""

from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatchcase
import re
from typing import Any

from mewcode_agent.hooks.models import HookCondition, HookValueMatcher


def matcher_matches(matcher: HookValueMatcher, value: Any) -> bool:
    if matcher.kind == "not":
        assert isinstance(matcher.pattern, HookValueMatcher)
        return not matcher_matches(matcher.pattern, value)
    if matcher.kind == "exact":
        pattern = matcher.pattern
        return type(value) is type(pattern) and value == pattern
    if not isinstance(value, str):
        return False
    assert isinstance(matcher.pattern, str)
    if matcher.kind == "glob":
        return fnmatchcase(value, matcher.pattern)
    return re.fullmatch(matcher.pattern, value) is not None


def condition_matches(
    condition: HookCondition,
    context: Mapping[str, Any],
) -> bool:
    results = (
        path in context and matcher_matches(matcher, context[path])
        for path, matcher in condition.matchers.items()
    )
    if condition.mode == "all":
        return all(results)
    return any(results)


def rule_matches(
    condition: HookCondition | None,
    context: Mapping[str, Any],
) -> bool:
    if condition is None:
        return True
    return condition_matches(condition, context)
