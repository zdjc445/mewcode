from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.skills import (
    SkillConfigError,
    builtin_skill_root,
    load_skill_definition,
    scan_skill_catalog,
)


def skill_document(
    *,
    name: str = "example",
    description: str = "Example skill",
    allowed_tools: tuple[str, ...] = ("read_file",),
    execution_mode: str = "shared",
    context_strategy: str = "current",
    recent_messages: str = "null",
    body: str = "Follow this SOP exactly.",
) -> str:
    tools = "\n".join(f"  - {item}" for item in allowed_tools)
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "allowed_tools:\n"
        f"{tools}\n"
        f"execution_mode: {execution_mode}\n"
        "model: inherit\n"
        f"context_strategy: {context_strategy}\n"
        f"recent_messages: {recent_messages}\n"
        "---\n"
        f"{body}\n"
    )


def write_skill(
    root: Path,
    relative: str,
    content: str,
) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_single_file_skill_with_exact_metadata(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    path = write_skill(root, "alias.md", skill_document())

    definition = load_skill_definition(
        path,
        source="project",
        source_root=root,
        directory_skill=False,
    )

    assert definition.name == "example"
    assert definition.description == "Example skill"
    assert definition.allowed_tools == ("read_file",)
    assert definition.execution_mode == "shared"
    assert definition.context_strategy == "current"
    assert definition.recent_messages is None
    assert definition.body == "Follow this SOP exactly."
    assert definition.source == "project"
    assert definition.source_path == path.resolve()
    assert definition.skill_directory is None
    assert definition.dedicated_tools == ()


def test_loads_crlf_frontmatter_and_recent_isolated_skill(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    text = skill_document(
        execution_mode="isolated",
        context_strategy="recent",
        recent_messages="7",
    ).replace("\n", "\r\n")
    path = write_skill(root, "example.md", text)

    definition = load_skill_definition(
        path,
        source="user",
        source_root=root,
        directory_skill=False,
    )

    assert definition.execution_mode == "isolated"
    assert definition.context_strategy == "recent"
    assert definition.recent_messages == 7


def test_directory_manifest_is_strict_and_resolves_script(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    entry = write_skill(root, "example/SKILL.md", skill_document(allowed_tools=("example_tool",)))
    script = write_skill(root, "example/tools/example_tool.py", "print('{}')\n")
    write_skill(
        root,
        "example/tools.yaml",
        """version: 1
tools:
  - name: example_tool
    description: Example tool
    parameters:
      type: object
      properties: {}
      additionalProperties: false
    category: command
    timeout_seconds: 30
    script: tools/example_tool.py
""",
    )

    definition = load_skill_definition(
        entry,
        source="project",
        source_root=root,
        directory_skill=True,
    )

    assert definition.skill_directory == entry.parent.resolve()
    assert len(definition.dedicated_tools) == 1
    tool = definition.dedicated_tools[0]
    assert tool.name == "example_tool"
    assert tool.category == "command"
    assert tool.timeout_seconds == 30.0
    assert tool.script == "tools/example_tool.py"
    assert tool.script_path == script.resolve()


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("name: example\n", "skill_document_invalid"),
        ("---\nname: example\n", "skill_document_invalid"),
        ("---\nname: example\n---\n", "skill_document_invalid"),
        (
            skill_document().replace(
                "description: Example skill\n",
                "description: one\ndescription: two\n",
            ),
            "skill_document_invalid",
        ),
        (
            skill_document().replace(
                "description: Example skill\n",
                "description: Example skill\nunknown: value\n",
            ),
            "skill_metadata_invalid",
        ),
        (
            skill_document(execution_mode="shared", context_strategy="none"),
            "skill_metadata_invalid",
        ),
        (
            skill_document(
                execution_mode="isolated",
                context_strategy="recent",
                recent_messages="null",
            ),
            "skill_metadata_invalid",
        ),
    ],
)
def test_rejects_invalid_skill_documents(
    tmp_path: Path,
    content: str,
    code: str,
) -> None:
    root = tmp_path / "skills"
    path = write_skill(root, "example.md", content)

    with pytest.raises(SkillConfigError) as caught:
        load_skill_definition(
            path,
            source="project",
            source_root=root,
            directory_skill=False,
        )

    assert caught.value.code == code


@pytest.mark.parametrize(
    "manifest",
    [
        "version: 2\ntools: []\n",
        "version: 1\ntools: {}\n",
        "version: 1\ntools: []\nunknown: true\n",
        """version: 1
tools:
  - name: example_tool
    description: Example tool
    parameters: {type: not-a-real-type}
    category: command
    timeout_seconds: 30
    script: tools/example_tool.py
""",
        """version: 1
tools:
  - name: example_tool
    description: Example tool
    parameters: {type: object}
    category: read
    timeout_seconds: 30
    script: tools/example_tool.py
""",
        """version: 1
tools:
  - name: example_tool
    description: Example tool
    parameters: {type: object}
    category: command
    timeout_seconds: 0
    script: tools/example_tool.py
""",
        """version: 1
tools:
  - name: example_tool
    description: Example tool
    parameters: {type: object}
    category: command
    timeout_seconds: 30
    script: ../outside.py
""",
        """version: 1
tools:
  - name: example_tool
    description: Example tool
    parameters:
      type: object
      properties:
        1: {type: string}
    category: command
    timeout_seconds: 30
    script: tools/example_tool.py
""",
    ],
)
def test_rejects_invalid_directory_manifests(
    tmp_path: Path,
    manifest: str,
) -> None:
    root = tmp_path / "skills"
    entry = write_skill(root, "example/SKILL.md", skill_document())
    write_skill(root, "example/tools/example_tool.py", "print('{}')\n")
    write_skill(root, "example/tools.yaml", manifest)

    with pytest.raises(SkillConfigError) as caught:
        load_skill_definition(
            entry,
            source="project",
            source_root=root,
            directory_skill=True,
        )

    assert caught.value.code in ("skill_manifest_invalid", "skill_path_invalid")


@pytest.mark.parametrize(
    "name",
    [
        "Uppercase",
        "has-hyphen",
        "_leading",
        "a" * 65,
    ],
)
def test_rejects_directory_tool_name_outside_exact_pattern(
    tmp_path: Path,
    name: str,
) -> None:
    root = tmp_path / "skills"
    entry = write_skill(root, "example/SKILL.md", skill_document())
    write_skill(root, "example/tools/example.py", "print('{}')\n")
    write_skill(
        root,
        "example/tools.yaml",
        f"""version: 1
tools:
  - name: {name}
    description: Example tool
    parameters: {{type: object}}
    category: command
    timeout_seconds: 30
    script: tools/example.py
""",
    )

    with pytest.raises(SkillConfigError) as caught:
        load_skill_definition(
            entry,
            source="project",
            source_root=root,
            directory_skill=True,
        )

    assert caught.value.code == "skill_manifest_invalid"


def test_catalog_applies_precedence_and_invalid_high_layer_fallback(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    project = tmp_path / "project"
    write_skill(builtin, "example.md", skill_document(description="Builtin"))
    write_skill(user / "skills", "example.md", skill_document(description="User"))
    write_skill(
        project / ".mewcode" / "skills",
        "example.md",
        "not frontmatter",
    )
    diagnostics = []

    snapshot = scan_skill_catalog(
        project_root=project,
        user_root=user,
        builtin_root=builtin,
        existing_tool_names=("read_file",),
        reserved_command_names=("help",),
        diagnostic_handler=diagnostics.append,
    )

    assert tuple(item.name for item in snapshot.definitions) == ("example",)
    assert snapshot.definitions[0].description == "User"
    assert snapshot.definitions[0].source == "user"
    assert len(snapshot.diagnostics) == 1
    assert diagnostics == list(snapshot.diagnostics)
    assert snapshot.diagnostics[0].source == "project"
    assert snapshot.diagnostics[0].candidate == "example.md"


def test_same_layer_duplicate_name_rejects_both_and_uses_lower_layer(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    project = tmp_path / "project"
    write_skill(builtin, "example.md", skill_document(description="Builtin"))
    write_skill(user / "skills", "a.md", skill_document(description="User A"))
    write_skill(user / "skills", "b.md", skill_document(description="User B"))

    snapshot = scan_skill_catalog(
        project_root=project,
        user_root=user,
        builtin_root=builtin,
        existing_tool_names=("read_file",),
        reserved_command_names=(),
    )

    assert snapshot.definitions[0].description == "Builtin"
    assert [item.code for item in snapshot.diagnostics] == [
        "skill_name_conflict",
        "skill_name_conflict",
    ]


def test_catalog_fails_fast_for_missing_tool_and_command_conflict(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    project = tmp_path / "project"
    user = tmp_path / "user"
    write_skill(builtin, "example.md", skill_document(allowed_tools=("missing",)))

    with pytest.raises(SkillConfigError) as missing:
        scan_skill_catalog(
            project_root=project,
            user_root=user,
            builtin_root=builtin,
            existing_tool_names=("read_file",),
            reserved_command_names=(),
        )
    assert missing.value.code == "skill_tool_missing"

    write_skill(builtin, "example.md", skill_document(allowed_tools=("read_file",)))
    with pytest.raises(SkillConfigError) as conflict:
        scan_skill_catalog(
            project_root=project,
            user_root=user,
            builtin_root=builtin,
            existing_tool_names=("read_file",),
            reserved_command_names=("example",),
        )
    assert conflict.value.code == "skill_name_conflict"


def test_catalog_rejects_dedicated_tool_conflicts(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    project = tmp_path / "project"
    user = tmp_path / "user"
    entry = write_skill(
        builtin,
        "example/SKILL.md",
        skill_document(allowed_tools=("read_file",)),
    )
    write_skill(entry.parent, "tools/example.py", "print('{}')\n")
    write_skill(
        entry.parent,
        "tools.yaml",
        """version: 1
tools:
  - name: read_file
    description: Conflict
    parameters: {type: object}
    category: command
    timeout_seconds: 30
    script: tools/example.py
""",
    )

    with pytest.raises(SkillConfigError) as caught:
        scan_skill_catalog(
            project_root=project,
            user_root=user,
            builtin_root=builtin,
            existing_tool_names=("read_file",),
            reserved_command_names=(),
        )

    assert caught.value.code == "skill_tool_conflict"


def test_catalog_rejects_team_system_tool_name_for_dedicated_tool(
    tmp_path: Path,
) -> None:
    builtin = tmp_path / "builtin"
    project = tmp_path / "project"
    user = tmp_path / "user"
    entry = write_skill(
        builtin,
        "example/SKILL.md",
        skill_document(allowed_tools=("read_file",)),
    )
    write_skill(entry.parent, "tools/example.py", "print('{}')\n")
    write_skill(
        entry.parent,
        "tools.yaml",
        """version: 1
tools:
  - name: team_status
    description: Conflict
    parameters: {type: object}
    category: command
    timeout_seconds: 30
    script: tools/example.py
""",
    )

    with pytest.raises(SkillConfigError) as caught:
        scan_skill_catalog(
            project_root=project,
            user_root=user,
            builtin_root=builtin,
            existing_tool_names=("read_file",),
            reserved_command_names=(),
        )

    assert caught.value.code == "skill_tool_conflict"


def test_builtin_catalog_contains_exact_productivity_templates(
    tmp_path: Path,
) -> None:
    snapshot = scan_skill_catalog(
        project_root=tmp_path / "project",
        user_root=tmp_path / "user",
        builtin_root=builtin_skill_root(),
        existing_tool_names=(
            "read_file",
            "find_files",
            "search_code",
            "run_command",
        ),
        reserved_command_names=("help", "skills"),
    )

    assert [item.name for item in snapshot.definitions] == [
        "commit",
        "review",
        "test",
    ]
    assert [item.execution_mode for item in snapshot.definitions] == [
        "shared",
        "isolated",
        "isolated",
    ]
    assert [item.context_strategy for item in snapshot.definitions] == [
        "current",
        "recent",
        "summary",
    ]
    assert snapshot.definitions[1].recent_messages == 12
