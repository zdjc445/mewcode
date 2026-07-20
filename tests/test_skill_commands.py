from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mewcode_agent.commands import (
    CommandController,
    CommandMode,
    CommandRegistry,
    ConfirmationRequest,
)
from mewcode_agent.prompting.environment import (
    GitEnvironment,
    RequestEnvironment,
    SessionEnvironment,
)
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.skills import (
    LoadSkillTool,
    SkillCatalog,
    SkillCommandManager,
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
    name = "read_file"
    description = "stub"
    parameters = {"type": "object"}
    category = "read"

    async def execute(self, arguments: dict[str, Any]) -> Any:
        return arguments


class RecordingUI:
    def __init__(self) -> None:
        self.messages: list[tuple[str, ...]] = []
        self.sent: list[tuple[str, CommandMode]] = []
        self.statuses: list[str] = []

    async def show_system_message(self, lines: tuple[str, ...]) -> None:
        self.messages.append(lines)

    async def request_confirmation(self, request: ConfirmationRequest) -> bool:
        return False

    async def send_user_message(
        self,
        message: str,
        *,
        mode: CommandMode,
    ) -> None:
        self.sent.append((message, mode))

    def get_default_mode(self) -> CommandMode:
        return "execute"

    def set_default_mode(self, mode: CommandMode) -> None:
        return None

    def clear_transcript(self) -> None:
        return None

    def refresh_status(self, state: str) -> None:
        self.statuses.append(state)


def skill_document(
    name: str,
    *,
    description: str | None = None,
    allowed_tool: str = "read_file",
) -> str:
    return f"""---
name: {name}
description: {description or name + ' description'}
allowed_tools:
  - {allowed_tool}
execution_mode: shared
model: inherit
context_strategy: current
recent_messages: null
---
{name.upper()} SOP SECRET
"""


def make_fixture(
    tmp_path: Path,
) -> tuple[
    SkillCommandManager,
    SkillRuntime,
    ToolRegistry,
    CommandRegistry,
    CommandController,
    RecordingUI,
    Path,
]:
    project_root = tmp_path / "project"
    skills_root = project_root / ".mewcode" / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / "alpha.md").write_text(
        skill_document("alpha"),
        encoding="utf-8",
    )
    builtin_root = tmp_path / "builtin"
    builtin_root.mkdir()
    registry = ToolRegistry()
    registry.register(StubTool())
    snapshot = scan_skill_catalog(
        project_root=project_root,
        user_root=tmp_path / "user",
        builtin_root=builtin_root,
        existing_tool_names=registry.tool_names(),
        reserved_command_names=("skills", "help"),
    )
    prompt_runtime = PromptRuntime(
        SessionEnvironment(
            "Windows",
            "powershell.exe",
            str(project_root.resolve()),
            "China Standard Time",
            "+08:00",
        ),
        FixedCollector(),
    )
    runtime = SkillRuntime(
        SkillCatalog(snapshot),
        registry,
        prompt_runtime,
        reserved_command_names=("skills", "help"),
    )
    registry.register(LoadSkillTool(runtime))
    manager = SkillCommandManager(
        project_root=project_root,
        user_root=tmp_path / "user",
        builtin_root=builtin_root,
        tool_registry=registry,
        skill_runtime=runtime,
        reserved_command_names=("skills", "help"),
    )
    commands = CommandRegistry()
    commands.register(manager.management_spec())
    commands.freeze()
    manager.bind_registry(commands)
    commands.replace_dynamic(manager.dynamic_specs())
    ui = RecordingUI()
    return (
        manager,
        runtime,
        registry,
        commands,
        CommandController(commands, ui),
        ui,
        skills_root,
    )


@pytest.mark.asyncio
async def test_skill_shortcut_sends_exact_load_request_in_execute_mode(
    tmp_path: Path,
) -> None:
    _manager, _runtime, _tools, commands, controller, ui, _root = (
        make_fixture(tmp_path)
    )

    result = await controller.dispatch("/alpha Exact/ARG Value")

    assert result.success is True
    assert ui.sent == [
        (
            "请使用 Skill `alpha` 完成任务。"
            "必须先调用 `load_skill`，并严格遵循加载后的完整 SOP。\n"
            "Skill 参数（原文）：\n"
            "Exact/ARG Value",
            "execute",
        )
    ]
    assert commands.completion_candidates("a") == ("alpha",)


@pytest.mark.asyncio
async def test_skills_list_and_show_do_not_disclose_sop(tmp_path: Path) -> None:
    _manager, _runtime, _tools, _commands, controller, ui, _root = (
        make_fixture(tmp_path)
    )

    listed = await controller.dispatch("/skills")
    shown = await controller.dispatch("/skills show alpha")

    assert listed.success is True
    assert shown.success is True
    text = "\n".join(line for message in ui.messages for line in message)
    assert "alpha description" in text
    assert "execution_mode: shared" in text
    assert "allowed_tools: read_file" in text
    assert "ALPHA SOP SECRET" not in text


@pytest.mark.asyncio
async def test_rescan_atomically_replaces_dynamic_commands_and_catalog(
    tmp_path: Path,
) -> None:
    _manager, runtime, _tools, commands, controller, ui, root = (
        make_fixture(tmp_path)
    )
    (root / "alpha.md").unlink()
    (root / "beta.md").write_text(
        skill_document("beta"),
        encoding="utf-8",
    )

    result = await controller.dispatch("/skills rescan")

    assert result.success is True
    assert runtime.catalog.get("alpha") is None
    assert runtime.catalog.get("beta") is not None
    assert commands.resolve("alpha") is None
    assert commands.resolve("beta") is not None
    assert "loaded=1" in ui.messages[-1][0]


@pytest.mark.asyncio
async def test_failed_rescan_keeps_commands_catalog_and_tools_unchanged(
    tmp_path: Path,
) -> None:
    _manager, runtime, tools, commands, controller, _ui, root = (
        make_fixture(tmp_path)
    )
    before_definitions = runtime.catalog.snapshot.definitions
    before_tools = tools.tool_names()
    (root / "alpha.md").write_text(
        skill_document("alpha", allowed_tool="missing_tool"),
        encoding="utf-8",
    )

    result = await controller.dispatch("/skills rescan")

    assert result.success is False
    assert runtime.catalog.snapshot.definitions == before_definitions
    assert tools.tool_names() == before_tools
    assert commands.resolve("alpha") is not None


@pytest.mark.asyncio
async def test_rescan_removes_deleted_active_skill(tmp_path: Path) -> None:
    _manager, runtime, _tools, _commands, controller, _ui, root = (
        make_fixture(tmp_path)
    )
    await runtime.load("alpha", "arg")
    assert len(runtime.active_skills) == 1
    (root / "alpha.md").unlink()

    result = await controller.dispatch("/skills rescan")

    assert result.success is True
    assert runtime.active_skills == ()
