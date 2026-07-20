from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mewcode_agent.skills import SkillScriptTool, SkillToolDefinition
from mewcode_agent.tools import Tool, ToolRegistry


class StubTool(Tool):
    description = "stub"
    parameters = {"type": "object"}
    category = "read"

    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, arguments: dict[str, Any]) -> Any:
        return arguments


def make_script_tool(
    tmp_path: Path,
    source: str,
    *,
    parameters: dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> SkillScriptTool:
    skill_directory = (tmp_path / "example").resolve()
    script_path = skill_directory / "tools" / "example.py"
    script_path.parent.mkdir(parents=True)
    script_path.write_text(source, encoding="utf-8")
    definition = SkillToolDefinition(
        "example_tool",
        "Example tool",
        parameters or {"type": "object"},
        "command",
        timeout_seconds,
        "tools/example.py",
        script_path,
    )
    return SkillScriptTool(definition, skill_directory=skill_directory)


@pytest.mark.asyncio
async def test_script_tool_uses_json_stdin_and_accepts_any_json_result(tmp_path: Path) -> None:
    tool = make_script_tool(
        tmp_path,
        """import json
import sys
arguments = json.load(sys.stdin)
json.dump({"cwd_name": __import__("pathlib").Path.cwd().name, "value": arguments["value"]}, sys.stdout)
""",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )

    result = await tool.execute({"value": 7})

    assert result == {"cwd_name": "example", "value": 7}


@pytest.mark.asyncio
async def test_script_tool_rejects_schema_before_starting_process(tmp_path: Path) -> None:
    marker = (tmp_path / "marker.txt").resolve()
    tool = make_script_tool(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\nprint('null')\n",
        parameters={
            "type": "object",
            "required": ["required"],
        },
    )
    registry = ToolRegistry()
    registry.replace_skill_tools((tool,))

    result = await registry.execute("example_tool", "{}")

    assert result.success is False
    assert result.error_code == "invalid_arguments"
    assert marker.exists() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "error_code"),
    [
        ("import sys\nsys.stderr.write('secret stderr')\nsys.exit(4)\n", "skill_script_failed"),
        ("print('not json')\n", "skill_script_output_invalid"),
        ("import sys\nsys.stdout.buffer.write(b'\\xff')\n", "skill_script_output_invalid"),
        ("print('NaN')\n", "skill_script_output_invalid"),
    ],
)
async def test_script_tool_returns_stable_sanitized_failures(
    tmp_path: Path,
    source: str,
    error_code: str,
) -> None:
    tool = make_script_tool(tmp_path, source)
    registry = ToolRegistry()
    registry.replace_skill_tools((tool,))

    result = await registry.execute("example_tool", "{}")

    assert result.success is False
    assert result.error_code == error_code
    assert "secret stderr" not in (result.error_message or "")


@pytest.mark.asyncio
async def test_registry_timeout_terminates_script_tool(tmp_path: Path) -> None:
    tool = make_script_tool(
        tmp_path,
        "import time\ntime.sleep(30)\nprint('null')\n",
        timeout_seconds=0.05,
    )
    registry = ToolRegistry()
    registry.replace_skill_tools((tool,))

    result = await registry.execute("example_tool", "{}")

    assert result.success is False
    assert result.error_code == "timeout"


def test_registry_replaces_skill_tools_without_losing_base_or_mcp() -> None:
    registry = ToolRegistry()
    base = StubTool("base")
    first = StubTool("first_skill")
    second = StubTool("second_skill")
    mcp = StubTool("mcp_server_012345678901234567890123")
    registry.register(base)
    registry.replace_skill_tools((first,))
    registry.replace_mcp_tools("server", (mcp,))

    registry.replace_skill_tools((second,))

    assert registry.tool_names() == (
        "base",
        "second_skill",
        "mcp_server_012345678901234567890123",
    )
    assert registry.get("first_skill") is None
    assert registry.get("second_skill") is second
    assert registry.get(mcp.name) is mcp


def test_registry_filters_provider_schema_by_exact_visible_names() -> None:
    registry = ToolRegistry()
    registry.register(StubTool("first"))
    registry.register(StubTool("second"))

    schemas = registry.api_tools(
        "anthropic",
        visible_names=frozenset({"second", "missing"}),
    )

    assert [item["name"] for item in schemas] == ["second"]


def test_registry_rejects_reserved_and_conflicting_skill_tool_names() -> None:
    registry = ToolRegistry()
    registry.register(StubTool("base"))

    with pytest.raises(ValueError, match="保留名称"):
        registry.replace_skill_tools((StubTool("load_skill"),))
    with pytest.raises(ValueError, match="保留名称"):
        registry.replace_skill_tools((StubTool("mcp_server_name"),))
    with pytest.raises(ValueError, match="冲突"):
        registry.replace_skill_tools((StubTool("base"),))

    assert registry.tool_names() == ("base",)
