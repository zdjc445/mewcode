from pathlib import Path
import re

import pytest

from mewcode_agent.hooks import (
    HookConfigError,
    HttpHookAction,
    PromptHookAction,
    ShellHookAction,
    SubagentHookAction,
    load_hook_configuration,
)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def rule_yaml(
    rule_id: str,
    *,
    event: str = "tool.before_execute",
    action: str | None = None,
    condition: str | None = None,
    run_async: str = "false",
    intercept: str = "null",
) -> str:
    action = action or """type: shell
      command: "echo ${tool.name}"
      cwd: project"""
    condition_yaml = (
        ""
        if condition is None
        else f"    condition:{condition}\n"
    )
    return f"""  - id: {rule_id}
    event: {event}
    once: false
    async: {run_async}
    timeout_seconds: 10
{condition_yaml}    action:
      {action}
    intercept: {intercept}
"""


def config(*rules: str) -> str:
    return "version: 1\nrules:\n" + "".join(rules)


def test_missing_hook_layers_are_empty(tmp_path: Path) -> None:
    loaded = load_hook_configuration(
        user_path=tmp_path / "user.yaml",
        project_path=tmp_path / "project.yaml",
    )

    assert loaded.rules == ()


def test_condition_and_execution_controls_are_optional(tmp_path: Path) -> None:
    user = tmp_path / "user.yaml"
    write(
        user,
        """version: 1
rules:
  - id: minimal
    event: system.startup
    action:
      type: shell
      command: exit 0
      cwd: project
""",
    )

    loaded = load_hook_configuration(
        user_path=user,
        project_path=tmp_path / "project.yaml",
    )

    rule = loaded.rules[0]
    assert rule.condition is None
    assert rule.once is False
    assert rule.run_async is False
    assert rule.timeout_seconds == 30
    assert rule.interception is None


def test_condition_requires_exactly_one_nonempty_all_or_any(
    tmp_path: Path,
) -> None:
    user = tmp_path / "user.yaml"
    write(
        user,
        config(
            rule_yaml(
                "mixed",
                condition="""
      all:
        tool.name:
          kind: exact
          pattern: write_file
      any:
        file.path:
          kind: glob
          pattern: 'src/**'""",
            )
        ),
    )

    with pytest.raises(HookConfigError, match="只能包含 all 或 any"):
        load_hook_configuration(
            user_path=user,
            project_path=tmp_path / "project.yaml",
        )


def test_project_rules_run_first_and_override_same_user_id(
    tmp_path: Path,
) -> None:
    user = tmp_path / "user.yaml"
    project = tmp_path / "project.yaml"
    write(
        user,
        config(rule_yaml("shared"), rule_yaml("user_only")),
    )
    write(
        project,
        config(rule_yaml("project_first"), rule_yaml("shared")),
    )

    loaded = load_hook_configuration(user_path=user, project_path=project)

    assert [rule.rule_id for rule in loaded.rules] == [
        "project_first",
        "shared",
        "user_only",
    ]
    assert [rule.source for rule in loaded.rules] == [
        "project",
        "project",
        "user",
    ]


def test_loads_all_four_action_types_and_recursive_not_matcher(
    tmp_path: Path,
) -> None:
    user = tmp_path / "user.yaml"
    write(
        user,
        config(
            rule_yaml("shell"),
            rule_yaml(
                "prompt",
                event="round.started",
                action='type: prompt\n      content: "round ${round.number}"',
            ),
            rule_yaml(
                "http",
                action="""type: http
      method: POST
      url: "https://example.test/${event.name}"
      headers:
        X-Event: "${event.name}"
      body: '{}'""",
                run_async="true",
            ),
            rule_yaml(
                "subagent",
                action="""type: subagent
      task: "inspect ${file.path}"
      context: recent""",
                condition="""
      all:
        file.path:
          kind: not
          pattern:
            kind: regex
            pattern: '.*\\.tmp'""",
            ),
        ),
    )

    loaded = load_hook_configuration(
        user_path=user,
        project_path=tmp_path / "missing.yaml",
    )

    assert isinstance(loaded.rules[0].action, ShellHookAction)
    assert isinstance(loaded.rules[1].action, PromptHookAction)
    assert isinstance(loaded.rules[2].action, HttpHookAction)
    assert isinstance(loaded.rules[3].action, SubagentHookAction)
    assert loaded.rules[3].condition is not None
    matcher = loaded.rules[3].condition.matchers["file.path"]
    assert matcher.kind == "not"
    assert matcher.pattern.kind == "regex"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("content", "location"),
    [
        (
            "version: 1\nversion: 1\nrules: []\n",
            "不是有效 YAML",
        ),
        (
            "version: 1\nrules: []\nextra: true\n",
            "包含未知字段",
        ),
        (
            config(rule_yaml("Bad-ID")),
            "rules[0]",
        ),
        (
            config(
                rule_yaml(
                    "bad_regex",
                    condition="""
      all:
        tool.name:
          kind: regex
          pattern: '['""",
                )
            ),
            "condition.all.tool.name.pattern",
        ),
        (
            config(
                rule_yaml(
                    "async_prompt",
                    event="round.started",
                    action="type: prompt\n      content: hello",
                    run_async="true",
                )
            ),
            "prompt action 不能异步执行",
        ),
        (
            config(
                rule_yaml(
                    "wrong_intercept",
                    event="round.started",
                    intercept="""
      deny: true
      reason: blocked""",
                )
            ),
            "intercept 只允许 tool.before_execute",
        ),
        (
            config(
                rule_yaml(
                    "bad_template",
                    action="type: shell\n      command: '${bad-key}'\n      cwd: project",
                )
            ),
            "action.command",
        ),
    ],
)
def test_invalid_hook_configuration_fails_with_precise_safe_location(
    tmp_path: Path,
    content: str,
    location: str,
) -> None:
    user = tmp_path / "user.yaml"
    write(user, content)

    with pytest.raises(HookConfigError, match=re.escape(location)) as captured:
        load_hook_configuration(
            user_path=user,
            project_path=tmp_path / "project.yaml",
        )

    assert "echo ${tool.name}" not in str(captured.value)


def test_duplicate_rule_id_in_one_layer_is_rejected(tmp_path: Path) -> None:
    user = tmp_path / "user.yaml"
    write(user, config(rule_yaml("same"), rule_yaml("same")))

    with pytest.raises(HookConfigError, match="重复规则 id"):
        load_hook_configuration(
            user_path=user,
            project_path=tmp_path / "project.yaml",
        )
