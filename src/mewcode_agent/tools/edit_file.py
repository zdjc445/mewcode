"""Ordered, unique text replacement tool."""

from __future__ import annotations

import asyncio
from typing import Any

from mewcode_agent.tools._paths import expand_path
from mewcode_agent.tools.base import Tool, ToolExecutionError, validate_arguments
from mewcode_agent.tools.diff import DiffResult, build_diff
from mewcode_agent.tools.file_state_cache import FileStateCache


class EditFileTool(Tool):
    category = "write"
    name = "edit_file"
    description = (
        "在 UTF-8 文本文件中按顺序进行多段原文替换。每项 old_text 必须在处理到该项时"
        "恰好出现一次；任意一项未匹配或多次匹配时，整个文件保持不变。"
        "文件必须先通过 read_file 读取且读取后未被修改。"
        "成功时返回统一 diff 和增删行数。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要修改的文件路径",
            },
            "edits": {
                "type": "array",
                "description": "按数组顺序执行的文本替换，至少包含一项",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {
                            "type": "string",
                            "description": "要唯一匹配的原文",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "替换后的文本",
                        },
                    },
                    "required": ["old_text", "new_text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["path", "edits"],
        "additionalProperties": False,
    }

    def __init__(self, file_state_cache: FileStateCache) -> None:
        self._file_state_cache = file_state_cache

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_arguments(
            arguments,
            required={"path": str, "edits": list},
        )
        path = expand_path(arguments["path"])
        edits = arguments["edits"]
        if not edits:
            raise ToolExecutionError(
                "invalid_arguments",
                "参数 edits 至少需要包含一项编辑",
            )

        validated_edits: list[tuple[str, str]] = []
        for index, edit_arguments in enumerate(edits):
            if not isinstance(edit_arguments, dict):
                raise ToolExecutionError(
                    "invalid_arguments",
                    f"参数 edits[{index}] 必须为 object",
                )
            try:
                validate_arguments(
                    edit_arguments,
                    required={"old_text": str, "new_text": str},
                )
            except ToolExecutionError as exc:
                raise ToolExecutionError(
                    exc.code,
                    f"参数 edits[{index}] 无效: {exc.message}",
                    details=exc.details,
                ) from exc

            old_text = edit_arguments["old_text"]
            if not old_text:
                raise ToolExecutionError(
                    "invalid_arguments",
                    f"参数 edits[{index}].old_text 不能为空",
                )
            validated_edits.append((old_text, edit_arguments["new_text"]))

        def edit() -> DiffResult:
            self._file_state_cache.ensure_current(path)
            old_content = path.read_text(encoding="utf-8")
            new_content = old_content
            for index, (old_text, new_text) in enumerate(validated_edits):
                match_count = new_content.count(old_text)
                if match_count == 0:
                    raise ToolExecutionError(
                        "text_not_found",
                        f"edits[{index}].old_text 在目标文件中没有匹配项",
                    )
                if match_count > 1:
                    raise ToolExecutionError(
                        "multiple_matches",
                        (
                            f"edits[{index}].old_text 在目标文件中匹配到 "
                            f"{match_count} 处，必须唯一匹配"
                        ),
                    )
                new_content = new_content.replace(old_text, new_text, 1)
            diff = build_diff(old_content, new_content)
            path.write_text(new_content, encoding="utf-8")
            self._file_state_cache.record(path)
            return diff

        diff = await asyncio.to_thread(edit)
        return {
            "path": str(path.resolve()),
            "replacements": len(validated_edits),
            "additions": diff.additions,
            "removals": diff.removals,
            "diff": diff.text,
            "diff_truncated": diff.truncated,
        }
