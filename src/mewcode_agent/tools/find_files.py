"""Glob-based file discovery tool."""

from __future__ import annotations

import asyncio
import glob
from pathlib import Path
from typing import Any

from mewcode_agent.tools._paths import expand_path
from mewcode_agent.tools.base import Tool, ToolExecutionError, validate_arguments


class FindFilesTool(Tool):
    category = "read"
    name = "find_files"
    description = (
        "按 glob 模式查找文件并返回绝对路径列表。支持 ** 递归模式，并包含隐藏文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "glob 文件模式，例如 **/*.py",
            },
            "path": {
                "type": "string",
                "description": "可选搜索根目录；默认使用当前工作目录",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_arguments(
            arguments,
            required={"pattern": str},
            optional={"path": str},
        )
        pattern = arguments["pattern"]
        if not pattern:
            raise ToolExecutionError(
                "invalid_arguments",
                "参数 pattern 不能为空",
            )
        root = expand_path(arguments["path"]) if "path" in arguments else Path.cwd()

        def find() -> list[str]:
            matches = glob.glob(
                str(root / pattern),
                recursive=True,
                include_hidden=True,
            )
            return sorted(
                str(Path(match).resolve())
                for match in matches
                if Path(match).is_file()
            )

        matches = await asyncio.to_thread(find)
        return {
            "path": str(root.resolve()),
            "pattern": pattern,
            "matches": matches,
        }
