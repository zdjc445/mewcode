from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mewcode_agent.prompting.environment import (
    GitEnvironment,
    RequestEnvironment,
    SessionEnvironment,
)
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.skills import (
    LoadSkillTool,
    SkillCatalog,
    SkillRuntime,
    scan_skill_catalog,
)
from mewcode_agent.tools import Tool, ToolRegistry


class FixedCollector:
    async def collect(self) -> RequestEnvironment:
        return RequestEnvironment(
            "2026-07-20T12:00:00+08:00",
            GitEnvironment("repository", "master", "", None),
        )


class StubTool(Tool):
    description = "stub"
    parameters = {"type": "object"}
    category = "read"

    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, arguments: dict[str, Any]) -> Any:
        return arguments


def make_prompt_runtime() -> PromptRuntime:
    return PromptRuntime(
        SessionEnvironment(
            "Windows",
            "powershell.exe",
            "D:\\workspace",
            "China Standard Time",
            "+08:00",
        ),
        FixedCollector(),
    )


def skill_document(
    name: str,
    description: str,
    allowed_tools: tuple[str, ...],
    body: str,
) -> str:
    tools = "\n".join(f"  - {item}" for item in allowed_tools)
    return f"""---
name: {name}
description: {description}
allowed_tools:
{tools}
execution_mode: shared
model: inherit
context_strategy: current
recent_messages: null
---
{body}
"""


def make_runtime(
    tmp_path: Path,
) -> tuple[SkillRuntime, ToolRegistry, PromptRuntime, Path]:
    project_root = tmp_path / "project"
    skills_root = project_root / ".mewcode" / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / "alpha.md").write_text(
        skill_document(
            "alpha",
            "Alpha description",
            ("read_file", "run_command"),
            "ALPHA SOP SECRET",
        ),
        encoding="utf-8",
    )
    (skills_root / "beta.md").write_text(
        skill_document(
            "beta",
            "Beta description",
            ("read_file",),
            "BETA SOP SECRET",
        ),
        encoding="utf-8",
    )
    builtin_root = tmp_path / "builtin"
    builtin_root.mkdir()
    registry = ToolRegistry()
    registry.register(StubTool("read_file"))
    registry.register(StubTool("run_command"))
    snapshot = scan_skill_catalog(
        project_root=project_root,
        user_root=tmp_path / "user",
        builtin_root=builtin_root,
        existing_tool_names=registry.tool_names(),
        reserved_command_names=("help", "skills"),
    )
    prompt_runtime = make_prompt_runtime()
    runtime = SkillRuntime(
        SkillCatalog(snapshot),
        registry,
        prompt_runtime,
        reserved_command_names=("help", "skills"),
    )
    registry.register(LoadSkillTool(runtime))
    return runtime, registry, prompt_runtime, skills_root


@pytest.mark.asyncio
async def test_catalog_only_discloses_name_and_description_until_activation(
    tmp_path: Path,
) -> None:
    runtime, _registry, prompt_runtime, _skills_root = make_runtime(tmp_path)

    catalog_content = prompt_runtime.timeline()[1].content

    assert "alpha" in catalog_content
    assert "Alpha description" in catalog_content
    assert "ALPHA SOP SECRET" not in catalog_content
    assert "allowed_tools" not in catalog_content
    assert runtime.active_skills == ()


@pytest.mark.asyncio
async def test_shared_activation_pins_hot_reloaded_sop_and_intersects_tools(
    tmp_path: Path,
) -> None:
    runtime, _registry, prompt_runtime, skills_root = make_runtime(tmp_path)
    (skills_root / "alpha.md").write_text(
        skill_document(
            "alpha",
            "Alpha description",
            ("read_file", "run_command"),
            "UPDATED ALPHA SOP",
        ),
        encoding="utf-8",
    )

    first = await runtime.load("alpha", "keep exact ARG")
    second = await runtime.load("beta", "")

    assert first == {
        "name": "alpha",
        "execution_mode": "shared",
        "active": True,
    }
    assert second["name"] == "beta"
    assert [item.definition.name for item in runtime.active_skills] == [
        "alpha",
        "beta",
    ]
    assert runtime.visible_tool_names() == frozenset(
        {"read_file", "load_skill"}
    )
    controls = prompt_runtime.timeline()
    active_content = "\n".join(item.content for item in controls[2:])
    assert "UPDATED ALPHA SOP" in active_content
    assert "ALPHA SOP SECRET" not in active_content
    assert "keep exact ARG" in active_content


@pytest.mark.asyncio
async def test_fork_current_clones_active_state_without_mutating_parent(
    tmp_path: Path,
) -> None:
    runtime, _registry, _prompt_runtime, _skills_root = make_runtime(tmp_path)
    await runtime.load("alpha", "parent args")
    child_prompt_runtime = make_prompt_runtime()

    child = runtime.fork_current(child_prompt_runtime)
    await child.load("beta", "child only")

    assert [item.definition.name for item in runtime.active_skills] == [
        "alpha"
    ]
    assert [item.definition.name for item in child.active_skills] == [
        "alpha",
        "beta",
    ]
    child_controls = "\n".join(
        item.content for item in child_prompt_runtime.timeline()
    )
    assert "parent args" in child_controls
    assert "child only" in child_controls


@pytest.mark.asyncio
async def test_load_skill_tool_uses_exact_name_and_reset_clears_activation(
    tmp_path: Path,
) -> None:
    runtime, registry, prompt_runtime, _skills_root = make_runtime(tmp_path)

    missing = await registry.execute(
        "load_skill",
        '{"name":"Alpha","arguments":""}',
    )
    loaded = await registry.execute(
        "load_skill",
        '{"name":"alpha","arguments":"x"}',
    )

    assert missing.success is False
    assert missing.error_code == "skill_not_found"
    assert loaded.success is True
    assert loaded.data["name"] == "alpha"
    runtime.reset_session()
    assert runtime.active_skills == ()
    assert len(prompt_runtime.timeline()) == 2
    assert prompt_runtime.timeline()[1].instruction_id == (
        "runtime.skills.catalog"
    )


@pytest.mark.asyncio
async def test_dedicated_tools_are_hidden_until_declared_skill_is_active(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    skill_root = project_root / ".mewcode" / "skills" / "scripted"
    (skill_root / "tools").mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        skill_document(
            "scripted",
            "Scripted",
            ("example_tool",),
            "Use the dedicated tool.",
        ),
        encoding="utf-8",
    )
    (skill_root / "tools" / "example.py").write_text(
        "print('null')\n",
        encoding="utf-8",
    )
    (skill_root / "tools.yaml").write_text(
        """version: 1
tools:
  - name: example_tool
    description: Example
    parameters: {type: object}
    category: command
    timeout_seconds: 30
    script: tools/example.py
""",
        encoding="utf-8",
    )
    builtin_root = tmp_path / "builtin"
    builtin_root.mkdir()
    registry = ToolRegistry()
    snapshot = scan_skill_catalog(
        project_root=project_root,
        user_root=tmp_path / "user",
        builtin_root=builtin_root,
        existing_tool_names=(),
        reserved_command_names=("skills",),
    )
    prompt_runtime = make_prompt_runtime()
    runtime = SkillRuntime(
        SkillCatalog(snapshot),
        registry,
        prompt_runtime,
        reserved_command_names=("skills",),
    )
    registry.register(LoadSkillTool(runtime))

    assert "example_tool" in registry.tool_names()
    assert "example_tool" not in runtime.visible_tool_names()

    await runtime.load("scripted", "")

    assert runtime.visible_tool_names() == frozenset(
        {"example_tool", "load_skill"}
    )
