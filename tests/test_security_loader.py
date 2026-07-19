from pathlib import Path

import pytest

from mewcode_agent.security import (
    SecurityConfigError,
    load_security_configuration,
)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_missing_security_layers_use_default_mode(tmp_path: Path) -> None:
    configuration = load_security_configuration(
        user_path=tmp_path / "user.yaml",
        project_path=tmp_path / "project.yaml",
    )

    assert configuration.mode == "default"
    assert configuration.user_rules == ()
    assert configuration.project_rules == ()


def test_loads_user_mode_and_exact_layer_scopes(tmp_path: Path) -> None:
    user = tmp_path / "user.yaml"
    project = tmp_path / "project.yaml"
    write(
        user,
        """version: 1
mode: strict
rules:
  - id: user.ask_commands
    action: ask
    tool: run_command
    priority: 10
    match: {}
""",
    )
    write(
        project,
        """version: 1
rules:
  - id: project.allow_tests
    action: allow
    tool: run_command
    priority: 20
    match:
      command:
        kind: glob
        pattern: "uv run pytest*"
""",
    )

    configuration = load_security_configuration(
        user_path=user,
        project_path=project,
    )

    assert configuration.mode == "strict"
    assert configuration.user_rules[0].scope == "user"
    assert configuration.project_rules[0].scope == "project"
    assert configuration.project_rules[0].matchers[0].kind == "glob"


@pytest.mark.parametrize(
    "content",
    [
        "version: 1\nmode: default\nmode: strict\nrules: []\n",
        "version: 1\nrules: []\nunknown: true\n",
        "version: 1\nrules:\n  - id: Bad-ID\n    action: allow\n"
        "    tool: read_file\n    priority: 1\n    match: {}\n",
    ],
)
def test_invalid_security_config_is_rejected(
    tmp_path: Path,
    content: str,
) -> None:
    user = tmp_path / "user.yaml"
    write(user, content)

    with pytest.raises(SecurityConfigError):
        load_security_configuration(
            user_path=user,
            project_path=tmp_path / "project.yaml",
        )


def test_project_layer_cannot_change_permission_mode(tmp_path: Path) -> None:
    project = tmp_path / "project.yaml"
    write(project, "version: 1\nmode: permissive\nrules: []\n")

    with pytest.raises(SecurityConfigError, match="未知字段"):
        load_security_configuration(
            user_path=tmp_path / "user.yaml",
            project_path=project,
        )
