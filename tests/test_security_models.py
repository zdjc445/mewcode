from pathlib import Path

import pytest

from mewcode_agent.security import (
    ArgumentMatcher,
    PolicyDecision,
    SecurityRequest,
    SecurityRule,
)


def test_security_request_repr_hides_arguments(tmp_path: Path) -> None:
    request = SecurityRequest(
        "call-1",
        "write_file",
        "write",
        {"path": "README.md", "content": "SECRET_BODY"},
        tmp_path.resolve(),
    )

    rendered = repr(request)

    assert "SECRET_BODY" not in rendered
    assert "README.md" not in rendered
    with pytest.raises(TypeError):
        request.arguments["path"] = "other.py"  # type: ignore[index]


def test_rule_rejects_duplicate_argument_matchers() -> None:
    with pytest.raises(ValueError, match="重复"):
        SecurityRule(
            "write.duplicate",
            "user",
            1,
            "ask",
            "write_file",
            (
                ArgumentMatcher("path", "path_glob", "src/**"),
                ArgumentMatcher("path", "exact", "src/app.py"),
            ),
        )


def test_fingerprint_rule_cannot_have_matchers() -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        SecurityRule(
            "approval.invalid",
            "user",
            0,
            "allow",
            "run_command",
            (ArgumentMatcher("command", "glob", "uv run pytest*"),),
            "a" * 64,
        )


def test_policy_decision_requires_rule_and_scope_together() -> None:
    with pytest.raises(ValueError, match="同时"):
        PolicyDecision("deny", "matched_rule", "rule.id", None)


def test_rule_rejects_non_string_fingerprint() -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        SecurityRule(
            "approval.invalid_type",
            "user",
            0,
            "allow",
            "run_command",
            fingerprint=123,  # type: ignore[arg-type]
        )
