import pytest

from mewcode_agent.hooks import (
    HookCondition,
    HookValueMatcher,
    matcher_matches,
    render_template,
    rule_matches,
    validate_template,
)


def test_matchers_are_type_sensitive_case_sensitive_and_full_value() -> None:
    assert matcher_matches(HookValueMatcher("exact", 1), 1)
    assert not matcher_matches(HookValueMatcher("exact", 1), True)
    assert matcher_matches(HookValueMatcher("glob", "src/*.py"), "src/a.py")
    assert not matcher_matches(
        HookValueMatcher("glob", "src/*.py"),
        "SRC/a.py",
    )
    assert matcher_matches(HookValueMatcher("regex", "a[0-9]+"), "a12")
    assert not matcher_matches(HookValueMatcher("regex", "a[0-9]+"), "xa12")


def test_not_reverses_child_but_missing_context_never_matches() -> None:
    matcher = HookValueMatcher(
        "not",
        HookValueMatcher("glob", "*.tmp"),
    )

    assert matcher_matches(matcher, "main.py")
    assert not matcher_matches(matcher, "cache.tmp")
    assert not rule_matches(HookCondition("all", {"file.path": matcher}), {})


def test_rule_match_uses_all_fields() -> None:
    matchers = {
        "event.name": HookValueMatcher("exact", "tool.before_execute"),
        "tool.name": HookValueMatcher("regex", "(?:write|edit)_file"),
    }

    condition = HookCondition("all", matchers)
    assert rule_matches(
        condition,
        {
            "event.name": "tool.before_execute",
            "tool.name": "write_file",
        },
    )
    assert not rule_matches(
        condition,
        {
            "event.name": "tool.before_execute",
            "tool.name": "read_file",
        },
    )


def test_any_condition_matches_when_exactly_one_field_matches() -> None:
    condition = HookCondition(
        "any",
        {
            "tool.name": HookValueMatcher("exact", "write_file"),
            "file.path": HookValueMatcher("glob", "docs/**"),
        },
    )

    assert rule_matches(condition, {"tool.name": "write_file"})
    assert rule_matches(condition, {"file.path": "docs/spec.md"})
    assert not rule_matches(condition, {"tool.name": "read_file"})
    assert rule_matches(None, {})


def test_template_renders_strings_and_compact_json_values() -> None:
    rendered = render_template(
        "${message.content}|${event.sequence}|${tool.result.data}",
        {
            "message.content": "原文",
            "event.sequence": 3,
            "tool.result.data": {"ok": True},
        },
    )

    assert rendered == '原文|3|{"ok":true}'


def test_template_missing_field_becomes_empty_and_syntax_is_strict() -> None:
    assert render_template("path=${file.path}", {}) == "path="
    with pytest.raises(ValueError, match="无效上下文字段"):
        validate_template("${File.Path}")
    with pytest.raises(ValueError, match="未闭合"):
        validate_template("${file.path")
