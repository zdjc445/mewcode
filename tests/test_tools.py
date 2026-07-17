from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from mewcode_agent.tools import (
    EditFileTool,
    FindFilesTool,
    ReadFileTool,
    RunCommandTool,
    SearchCodeTool,
    Tool,
    ToolRegistry,
    WriteFileTool,
    create_core_registry,
)


@pytest.mark.asyncio
async def test_read_and_write_file_tools(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "example.txt"
    registry = create_core_registry()

    written = await registry.execute(
        "write_file",
        json.dumps({"path": str(path), "content": "第一行\n第二行"}),
    )
    read = await registry.execute(
        "read_file",
        json.dumps({"path": str(path)}),
    )

    assert written.success is True
    assert written.data["characters_written"] == 7
    assert read.success is True
    assert read.data == {
        "path": str(path.resolve()),
        "content": "第一行\n第二行",
        "offset": 0,
        "limit": 2000,
        "total_lines": 2,
        "has_more": False,
        "next_offset": 2,
    }


@pytest.mark.asyncio
async def test_read_file_supports_line_pagination(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text(
        "line 1\nline 2\nline 3\nline 4\nline 5\n",
        encoding="utf-8",
    )
    registry = create_core_registry()

    first = await registry.execute(
        "read_file",
        json.dumps({"path": str(path), "offset": 1, "limit": 2}),
    )
    last = await registry.execute(
        "read_file",
        json.dumps({"path": str(path), "offset": 3, "limit": 2}),
    )
    beyond_end = await registry.execute(
        "read_file",
        json.dumps({"path": str(path), "offset": 8, "limit": 2}),
    )

    assert first.success is True
    assert first.data == {
        "path": str(path.resolve()),
        "content": "line 2\nline 3\n",
        "offset": 1,
        "limit": 2,
        "total_lines": 5,
        "has_more": True,
        "next_offset": 3,
    }
    assert last.success is True
    assert last.data["content"] == "line 4\nline 5\n"
    assert last.data["has_more"] is False
    assert last.data["next_offset"] == 5
    assert beyond_end.success is True
    assert beyond_end.data["content"] == ""
    assert beyond_end.data["has_more"] is False
    assert beyond_end.data["next_offset"] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pagination",
    [
        {"offset": -1},
        {"offset": True},
        {"limit": 0},
        {"limit": 2001},
        {"limit": False},
    ],
)
async def test_read_file_rejects_invalid_pagination(
    tmp_path: Path,
    pagination: dict[str, Any],
) -> None:
    path = tmp_path / "example.txt"
    path.write_text("content", encoding="utf-8")

    result = await create_core_registry().execute(
        "read_file",
        json.dumps({"path": str(path), **pagination}),
    )

    assert result.success is False
    assert result.error_code == "invalid_arguments"


@pytest.mark.asyncio
async def test_edit_file_applies_multiple_exact_matches_in_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "example.txt"
    path.write_text("first\nmiddle\nlast\n", encoding="utf-8")
    registry = create_core_registry()

    result = await registry.execute(
        "edit_file",
        json.dumps(
            {
                "path": str(path),
                "edits": [
                    {"old_text": "first", "new_text": "new first"},
                    {"old_text": "last", "new_text": "new last"},
                ],
            }
        ),
    )

    assert result.success is True
    assert result.data["replacements"] == 2
    assert result.data["additions"] == 2
    assert result.data["removals"] == 2
    assert result.data["diff_truncated"] is False
    assert "-first" in result.data["diff"]
    assert "+new first" in result.data["diff"]
    assert "-last" in result.data["diff"]
    assert "+new last" in result.data["diff"]
    assert path.read_text(encoding="utf-8") == "new first\nmiddle\nnew last\n"


@pytest.mark.asyncio
async def test_edit_file_each_edit_sees_previous_edit_output(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("old", encoding="utf-8")

    result = await create_core_registry().execute(
        "edit_file",
        json.dumps(
            {
                "path": str(path),
                "edits": [
                    {"old_text": "old", "new_text": "intermediate"},
                    {"old_text": "intermediate", "new_text": "final"},
                ],
            }
        ),
    )

    assert result.success is True
    assert result.data["replacements"] == 2
    assert result.data["additions"] == 1
    assert result.data["removals"] == 1
    assert "-old" in result.data["diff"]
    assert "+final" in result.data["diff"]
    assert path.read_text(encoding="utf-8") == "final"


@pytest.mark.asyncio
async def test_edit_file_returns_empty_diff_for_unchanged_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "example.txt"
    path.write_text("unchanged\n", encoding="utf-8")

    result = await create_core_registry().execute(
        "edit_file",
        json.dumps(
            {
                "path": str(path),
                "edits": [
                    {"old_text": "unchanged", "new_text": "unchanged"},
                ],
            }
        ),
    )

    assert result.success is True
    assert result.data["additions"] == 0
    assert result.data["removals"] == 0
    assert result.data["diff"] == ""
    assert result.data["diff_truncated"] is False


@pytest.mark.asyncio
async def test_edit_file_truncates_large_diff(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    old_content = "\n".join(f"old {index}" for index in range(250)) + "\n"
    new_content = "\n".join(f"new {index}" for index in range(250)) + "\n"
    path.write_text(old_content, encoding="utf-8")

    result = await create_core_registry().execute(
        "edit_file",
        json.dumps(
            {
                "path": str(path),
                "edits": [
                    {"old_text": old_content, "new_text": new_content},
                ],
            }
        ),
    )

    assert result.success is True
    assert result.data["additions"] == 250
    assert result.data["removals"] == 250
    assert result.data["diff_truncated"] is True
    assert result.data["diff"].endswith(
        "... diff 已截断，只显示前 200 行"
    )
    assert path.read_text(encoding="utf-8") == new_content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "old_text", "error_code"),
    [
        ("unchanged", "missing", "text_not_found"),
        ("same same", "same", "multiple_matches"),
        ("unchanged", "", "invalid_arguments"),
    ],
)
async def test_edit_file_returns_clear_match_errors(
    tmp_path: Path,
    content: str,
    old_text: str,
    error_code: str,
) -> None:
    path = tmp_path / "example.txt"
    path.write_text(content, encoding="utf-8")

    result = await create_core_registry().execute(
        "edit_file",
        json.dumps(
            {
                "path": str(path),
                "edits": [{"old_text": old_text, "new_text": "new"}],
            }
        ),
    )

    assert result.success is False
    assert result.error_code == error_code
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_edit_file_does_not_write_when_a_later_edit_fails(
    tmp_path: Path,
) -> None:
    path = tmp_path / "example.txt"
    original = "first\nmiddle\nlast\n"
    path.write_text(original, encoding="utf-8")

    result = await create_core_registry().execute(
        "edit_file",
        json.dumps(
            {
                "path": str(path),
                "edits": [
                    {"old_text": "first", "new_text": "changed"},
                    {"old_text": "missing", "new_text": "new"},
                ],
            }
        ),
    )

    assert result.success is False
    assert result.error_code == "text_not_found"
    assert "edits[1]" in result.error_message
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edits",
    [
        [],
        ["not an object"],
        [{"old_text": "old"}],
        [{"old_text": "old", "new_text": "new", "extra": True}],
        [{"old_text": 1, "new_text": "new"}],
    ],
)
async def test_edit_file_rejects_invalid_edit_items(
    tmp_path: Path,
    edits: list[Any],
) -> None:
    path = tmp_path / "example.txt"
    path.write_text("old", encoding="utf-8")

    result = await create_core_registry().execute(
        "edit_file",
        json.dumps({"path": str(path), "edits": edits}),
    )

    assert result.success is False
    assert result.error_code == "invalid_arguments"
    assert path.read_text(encoding="utf-8") == "old"


@pytest.mark.asyncio
async def test_find_files_and_search_code(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "pkg" / "one.py").write_text(
        "first\nTARGET = 1\n",
        encoding="utf-8",
    )
    (tmp_path / ".hidden" / "two.py").write_text(
        "TARGET = 2\n",
        encoding="utf-8",
    )
    (tmp_path / "ignore.txt").write_text("TARGET = 3\n", encoding="utf-8")
    registry = create_core_registry()

    found = await registry.execute(
        "find_files",
        json.dumps({"path": str(tmp_path), "pattern": "**/*.py"}),
    )
    searched = await registry.execute(
        "search_code",
        json.dumps(
            {
                "path": str(tmp_path),
                "file_pattern": "**/*.py",
                "query": r"TARGET\s*=",
            }
        ),
    )

    assert found.success is True
    assert found.data["matches"] == sorted(
        [
            str((tmp_path / ".hidden" / "two.py").resolve()),
            str((tmp_path / "pkg" / "one.py").resolve()),
        ]
    )
    assert searched.success is True
    assert [(match["line"], match["content"]) for match in searched.data["matches"]] == [
        (1, "TARGET = 2"),
        (2, "TARGET = 1"),
    ]


@pytest.mark.asyncio
async def test_search_code_rejects_invalid_regular_expression() -> None:
    result = await create_core_registry().execute(
        "search_code",
        json.dumps({"query": "["}),
    )

    assert result.success is False
    assert result.error_code == "invalid_regular_expression"


@pytest.mark.asyncio
async def test_run_command_returns_output_and_nonzero_failure(tmp_path: Path) -> None:
    registry = create_core_registry()
    success = await registry.execute(
        "run_command",
        json.dumps(
            {
                "command": f'{sys.executable} -c "print(123)"',
                "cwd": str(tmp_path),
            }
        ),
    )
    failure = await registry.execute("run_command", json.dumps({"command": "exit 7"}))

    assert success.success is True
    assert success.data["exit_code"] == 0
    assert success.data["stdout"].strip() == "123"
    assert failure.success is False
    assert failure.error_code == "command_failed"
    assert failure.data["exit_code"] == 7


class SlowTool(Tool):
    name = "slow"
    description = "test"
    parameters = {"type": "object", "properties": {}}
    timeout_seconds = 0.01

    async def execute(self, arguments: dict[str, Any]) -> None:
        await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_registry_handles_lookup_json_validation_and_timeout() -> None:
    registry = ToolRegistry()
    registry.register(SlowTool())

    missing = await registry.execute("missing", "{}")
    invalid_json = await registry.execute("slow", "{")
    invalid_object = await registry.execute("slow", "[]")
    timeout = await registry.execute("slow", "{}")

    assert missing.error_code == "tool_not_found"
    assert invalid_json.error_code == "invalid_json"
    assert invalid_object.error_code == "invalid_arguments"
    assert timeout.error_code == "timeout"


def test_registry_exposes_six_tools_in_both_protocol_formats() -> None:
    registry = create_core_registry()
    expected_names = [
        ReadFileTool.name,
        WriteFileTool.name,
        EditFileTool.name,
        RunCommandTool.name,
        FindFilesTool.name,
        SearchCodeTool.name,
    ]

    openai_tools = registry.api_tools("openai")
    anthropic_tools = registry.api_tools("anthropic")

    assert [tool["function"]["name"] for tool in openai_tools] == expected_names
    assert [tool["name"] for tool in anthropic_tools] == expected_names
    assert openai_tools[0]["function"]["parameters"] == ReadFileTool.parameters
    assert anthropic_tools[0]["input_schema"] == ReadFileTool.parameters
