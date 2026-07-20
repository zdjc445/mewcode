"""Context matching for declarative Hook rules."""

from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatchcase
import re
from typing import Any

from mewcode_agent.hooks.models import HookValueMatcher


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


def rule_matches(
    matchers: Mapping[str, HookValueMatcher],
    context: Mapping[str, Any],
) -> bool:
    for path, matcher in matchers.items():
        if path not in context:
            return False
        if not matcher_matches(matcher, context[path]):
            return False
    return True
