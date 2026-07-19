from pathlib import Path

import pytest

from mewcode_agent.prompting.loader import (
    PromptConfigError,
    load_prompt_modules,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_missing_layers_return_sorted_builtins(tmp_path: Path) -> None:
    modules = load_prompt_modules(
        user_path=tmp_path / "user.yaml",
        project_path=tmp_path / "project.yaml",
    )

    assert modules == tuple(
        sorted(modules, key=lambda item: (item.priority, item.module_id))
    )
    assert "core.identity" in {item.module_id for item in modules}


def test_project_layer_exactly_overrides_and_disables_user_layer(
    tmp_path: Path,
) -> None:
    user_path = tmp_path / "user.yaml"
    project_path = tmp_path / "project.yaml"
    _write(
        user_path,
        """\
version: 1
modules:
  - id: coding.team
    enabled: true
    priority: 520
    content: user rule
  - id: output.default_style
    enabled: true
    priority: 810
    content: user output
""",
    )
    _write(
        project_path,
        """\
version: 1
modules:
  - id: coding.team
    enabled: true
    priority: 510
    content: project rule
  - id: output.default_style
    enabled: false
""",
    )

    modules = load_prompt_modules(
        user_path=user_path,
        project_path=project_path,
    )
    by_id = {item.module_id: item for item in modules}

    assert by_id["coding.team"].content == "project rule"
    assert by_id["coding.team"].source == "project"
    assert "output.default_style" not in by_id


def test_equal_priorities_use_exact_id_as_tiebreaker(tmp_path: Path) -> None:
    project_path = tmp_path / "project.yaml"
    _write(
        project_path,
        """\
version: 1
modules:
  - id: project.zeta
    enabled: true
    priority: 450
    content: z
  - id: project.alpha
    enabled: true
    priority: 450
    content: a
""",
    )

    modules = load_prompt_modules(
        user_path=tmp_path / "missing.yaml",
        project_path=project_path,
    )

    same_priority = [
        item.module_id for item in modules if item.priority == 450
    ]
    assert same_priority == ["project.alpha", "project.zeta"]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("version: 2\nmodules: []\n", "version"),
        ("version: 1\nmodules: {}\n", "modules"),
        ("version: 1\nmodules: []\nextra: true\n", "未知字段"),
        (
            "version: 1\nmodules:\n  - id: Coding.Team\n    enabled: true\n"
            "    priority: 1\n    content: x\n",
            "modules[0].id",
        ),
        (
            "version: 1\nmodules:\n  - id: core.safety\n    enabled: false\n",
            "core",
        ),
        (
            "version: 1\nmodules:\n  - id: missing.module\n    enabled: false\n",
            "不存在",
        ),
    ],
)
def test_invalid_project_config_reports_exact_path_without_content(
    tmp_path: Path,
    body: str,
    field: str,
) -> None:
    project_path = tmp_path / "prompts.yaml"
    _write(project_path, body)

    with pytest.raises(PromptConfigError) as exc_info:
        load_prompt_modules(
            user_path=tmp_path / "missing.yaml",
            project_path=project_path,
        )

    message = str(exc_info.value)
    assert "项目 Prompt 配置" in message
    assert str(project_path) in message
    assert field in message


def test_duplicate_ids_are_rejected_in_one_file(tmp_path: Path) -> None:
    project_path = tmp_path / "prompts.yaml"
    _write(
        project_path,
        """\
version: 1
modules:
  - id: project.rule
    enabled: true
    priority: 1
    content: first
  - id: project.rule
    enabled: true
    priority: 2
    content: second
""",
    )

    with pytest.raises(PromptConfigError, match="重复 id"):
        load_prompt_modules(
            user_path=tmp_path / "missing.yaml",
            project_path=project_path,
        )


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            "version: 1\nmodules:\n  - id: project.rule\n"
            "    enabled: true\n    priority: 1\n",
            "content",
        ),
        (
            "version: 1\nmodules:\n  - id: project.rule\n"
            "    enabled: true\n    priority: 1\n    content: x\n"
            "    extra: true\n",
            "未知字段",
        ),
        (
            "version: 1\nmodules:\n  - id: output.default_style\n"
            "    enabled: false\n    priority: 1\n",
            "未知字段",
        ),
        (
            "version: 1\nmodules:\n  - id: project.rule\n"
            "    enabled: yes\n    priority: 1\n    content: x\n",
            "enabled",
        ),
        (
            "version: 1\nmodules:\n  - id: project.rule\n"
            "    enabled: true\n    priority: true\n    content: x\n",
            "priority",
        ),
        (
            "version: 1\nmodules:\n  - id: project.rule\n"
            "    enabled: true\n    priority: 1\n    content: '   '\n",
            "content",
        ),
    ],
)
def test_enabled_and_disabled_entries_use_exact_field_sets(
    tmp_path: Path,
    body: str,
    field: str,
) -> None:
    project_path = tmp_path / "prompts.yaml"
    _write(project_path, body)

    with pytest.raises(PromptConfigError) as exc_info:
        load_prompt_modules(
            user_path=tmp_path / "missing.yaml",
            project_path=project_path,
        )

    assert str(project_path) in str(exc_info.value)
    assert field in str(exc_info.value)


def test_existing_config_path_must_be_a_file(tmp_path: Path) -> None:
    project_path = tmp_path / "prompts.yaml"
    project_path.mkdir()

    with pytest.raises(PromptConfigError, match="不是文件"):
        load_prompt_modules(
            user_path=tmp_path / "missing.yaml",
            project_path=project_path,
        )


def test_loader_api_is_exported_from_prompting_package() -> None:
    from mewcode_agent import prompting

    assert prompting.PromptConfigError is PromptConfigError
    assert prompting.load_prompt_modules is load_prompt_modules
