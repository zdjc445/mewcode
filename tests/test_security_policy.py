from pathlib import Path

import pytest

from mewcode_agent.security import (
    ArgumentMatcher,
    PathSandbox,
    PermanentApprovalStore,
    SecurityBoundary,
    SecurityConfiguration,
    SecurityPolicyEngine,
    SecurityRequest,
    SecurityRule,
)


def request(
    root: Path,
    *,
    tool: str = "run_command",
    category: str = "command",
    arguments: dict[str, object] | None = None,
    authorized: bool = False,
) -> SecurityRequest:
    return SecurityRequest(
        "call-1",
        tool,
        category,  # type: ignore[arg-type]
        arguments or {"command": "uv run pytest -q"},
        root.resolve(),
        authorized,
    )


def engine(
    root: Path,
    *,
    mode: str = "default",
    user_rules: tuple[SecurityRule, ...] = (),
    project_rules: tuple[SecurityRule, ...] = (),
    store: PermanentApprovalStore | None = None,
) -> SecurityPolicyEngine:
    return SecurityPolicyEngine(
        SecurityConfiguration(
            mode,  # type: ignore[arg-type]
            user_rules,
            project_rules,
        ),
        SecurityBoundary(PathSandbox(root)),
        approval_store=store,
    )


def rule(
    rule_id: str,
    scope: str,
    action: str,
    *,
    priority: int = 0,
    matchers: tuple[ArgumentMatcher, ...] = (),
) -> SecurityRule:
    return SecurityRule(
        rule_id,
        scope,  # type: ignore[arg-type]
        priority,
        action,  # type: ignore[arg-type]
        "run_command",
        matchers,
    )


def test_hard_deny_cannot_be_overridden_by_permissive_or_allow_rule(
    tmp_path: Path,
) -> None:
    allow = rule("user.allow_all", "user", "allow", priority=999)
    policy = engine(tmp_path, mode="permissive", user_rules=(allow,))

    decision = policy.evaluate(
        request(tmp_path, arguments={"command": "git reset --hard HEAD"})
    )

    assert decision.action == "deny"
    assert decision.reason_code == "destructive_git_operation"


def test_rule_layer_order_is_session_then_project_then_user(
    tmp_path: Path,
) -> None:
    user_deny = rule("user.deny", "user", "deny", priority=100)
    project_ask = rule("project.ask", "project", "ask", priority=0)
    policy = engine(
        tmp_path,
        user_rules=(user_deny,),
        project_rules=(project_ask,),
    )
    call = request(tmp_path)

    assert policy.evaluate(call).action == "ask"
    assert policy.evaluate(call).rule_id == "project.ask"

    policy.allow_for_session(call)
    decision = policy.evaluate(call)
    assert decision.action == "allow"
    assert decision.scope == "session"


def test_same_layer_uses_priority_then_deny_ask_allow_then_id(
    tmp_path: Path,
) -> None:
    rules = (
        rule("user.allow", "user", "allow", priority=10),
        rule("user.ask", "user", "ask", priority=10),
        rule("user.deny_b", "user", "deny", priority=10),
        rule("user.deny_a", "user", "deny", priority=10),
    )

    decision = engine(tmp_path, user_rules=rules).evaluate(request(tmp_path))

    assert decision.action == "deny"
    assert decision.rule_id == "user.deny_a"


@pytest.mark.parametrize(
    ("mode", "category", "expected"),
    [
        ("strict", "read", "ask"),
        ("strict", "write", "ask"),
        ("default", "read", "allow"),
        ("default", "write", "ask"),
        ("default", "command", "ask"),
        ("permissive", "command", "allow"),
    ],
)
def test_permission_mode_only_supplies_unmatched_default(
    tmp_path: Path,
    mode: str,
    category: str,
    expected: str,
) -> None:
    tool = "read_file" if category == "read" else "write_file"
    if category == "command":
        tool = "run_command"
    arguments = (
        {"command": "echo safe"}
        if category == "command"
        else {"path": "README.md"}
    )

    decision = engine(tmp_path, mode=mode).evaluate(
        request(
            tmp_path,
            tool=tool,
            category=category,
            arguments=arguments,
        )
    )

    assert decision.action == expected


def test_runtime_mode_override_is_process_local_and_rules_still_win(
    tmp_path: Path,
) -> None:
    explicit_ask = rule("project.ask", "project", "ask")
    policy = engine(
        tmp_path,
        mode="strict",
        project_rules=(explicit_ask,),
    )
    unmatched = request(
        tmp_path,
        tool="write_file",
        category="write",
        arguments={"path": "README.md"},
    )

    policy.set_mode_override("permissive")

    assert policy.configured_mode == "strict"
    assert policy.mode == "permissive"
    assert policy.evaluate(unmatched).action == "allow"
    assert policy.evaluate(request(tmp_path)).action == "ask"
    status = policy.status()
    assert status.configured_mode == "strict"
    assert status.effective_mode == "permissive"
    assert status.has_runtime_override is True
    assert status.project_rule_count == 1

    policy.set_mode_override(None)
    assert policy.mode == "strict"
    assert policy.status().has_runtime_override is False


def test_request_authorization_does_not_override_explicit_ask(
    tmp_path: Path,
) -> None:
    ask = rule("project.confirm", "project", "ask")
    policy = engine(tmp_path, project_rules=(ask,))

    decision = policy.evaluate(request(tmp_path, authorized=True))

    assert decision.action == "ask"
    assert decision.rule_id == "project.confirm"


def test_path_glob_matches_canonical_workspace_relative_path(
    tmp_path: Path,
) -> None:
    allow = SecurityRule(
        "project.allow_src",
        "project",
        1,
        "allow",
        "write_file",
        (ArgumentMatcher("path", "path_glob", "src/*"),),
    )
    policy = engine(tmp_path, project_rules=(allow,))

    decision = policy.evaluate(
        request(
            tmp_path,
            tool="write_file",
            category="write",
            arguments={"path": "src/app.py", "content": "new"},
        )
    )

    assert decision.action == "allow"
    assert decision.rule_id == "project.allow_src"


def test_path_glob_double_star_matches_nested_components(
    tmp_path: Path,
) -> None:
    allow = SecurityRule(
        "project.allow_python",
        "project",
        1,
        "allow",
        "write_file",
        (ArgumentMatcher("path", "path_glob", "src/**/*.py"),),
    )
    policy = engine(tmp_path, project_rules=(allow,))

    decision = policy.evaluate(
        request(
            tmp_path,
            tool="write_file",
            category="write",
            arguments={"path": "src/pkg/app.py", "content": "new"},
        )
    )

    assert decision.action == "allow"


def test_path_glob_does_not_treat_non_path_argument_as_path(
    tmp_path: Path,
) -> None:
    invalid_match = SecurityRule(
        "project.invalid_path_matcher",
        "project",
        1,
        "allow",
        "run_command",
        (ArgumentMatcher("command", "path_glob", "**"),),
    )
    policy = engine(tmp_path, project_rules=(invalid_match,))

    decision = policy.evaluate(request(tmp_path))

    assert decision.action == "ask"


def test_permanent_approval_stores_only_fingerprint_not_command(
    tmp_path: Path,
) -> None:
    approval_path = tmp_path / "home" / "security-approvals.yaml"
    store = PermanentApprovalStore(approval_path)
    policy = engine(tmp_path, store=store)
    call = request(
        tmp_path,
        arguments={"command": "echo SECRET_COMMAND_VALUE"},
    )

    policy.allow_permanently(call)

    content = approval_path.read_text(encoding="utf-8")
    assert "SECRET_COMMAND_VALUE" not in content
    assert "fingerprint:" in content
    assert policy.evaluate(call).action == "allow"

    reloaded = SecurityPolicyEngine(
        SecurityConfiguration("default", (), (), store.load()),
        SecurityBoundary(PathSandbox(tmp_path)),
    )
    assert reloaded.evaluate(call).action == "allow"


def test_permanent_approval_is_bound_to_exact_project_root(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    store = PermanentApprovalStore(tmp_path / "home" / "approvals.yaml")
    first_policy = engine(first_root, store=store)
    first_call = request(
        first_root,
        arguments={"command": "echo project-scoped"},
    )
    first_policy.allow_permanently(first_call)

    second_policy = SecurityPolicyEngine(
        SecurityConfiguration("default", (), (), store.load()),
        SecurityBoundary(PathSandbox(second_root)),
    )
    second_call = request(
        second_root,
        arguments={"command": "echo project-scoped"},
    )

    assert second_policy.evaluate(second_call).action == "ask"


def test_write_approval_fingerprint_ignores_file_content(tmp_path: Path) -> None:
    policy = engine(tmp_path)
    first = request(
        tmp_path,
        tool="write_file",
        category="write",
        arguments={"path": "src/app.py", "content": "first"},
    )
    second = request(
        tmp_path,
        tool="write_file",
        category="write",
        arguments={"path": "src/app.py", "content": "second"},
    )

    assert policy.fingerprint(first) == policy.fingerprint(second)
