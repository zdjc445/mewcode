"""Regular-expression code search tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import Any

from mewcode_agent.tools._paths import expand_path
from mewcode_agent.tools.base import Tool, ToolExecutionError, validate_arguments


class SearchCodeTool(Tool):
    category = "read"
    name = "search_code"
    description = (
        "使用 Python 正则表达式搜索 UTF-8 文本文件内容，返回文件、行号和匹配行。"
        "无法按 UTF-8 读取的文件会被跳过。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Python 正则表达式"},
            "path": {
                "type": "string",
                "description": "可选搜索根目录或单个文件；默认使用当前工作目录",
            },
            "file_pattern": {
                "type": "string",
                "description": "可选 glob 文件模式；默认 **/*",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_arguments(
            arguments,
            required={"query": str},
            optional={"path": str, "file_pattern": str},
        )
        query = arguments["query"]
        root = expand_path(arguments["path"]) if "path" in arguments else Path.cwd()
        file_pattern = arguments.get("file_pattern", "**/*")
        try:
            expression = re.compile(query)
        except re.error as exc:
            raise ToolExecutionError(
                "invalid_regular_expression",
                f"query 不是有效的正则表达式: {exc}",
            ) from exc

        def search() -> list[dict[str, Any]]:
            paths = [root] if root.is_file() else root.glob(file_pattern)
            matches: list[dict[str, Any]] = []
            for candidate in paths:
                if not candidate.is_file():
                    continue
                try:
                    content = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                for line_number, line in enumerate(content.splitlines(), start=1):
                    if expression.search(line):
                        matches.append(
                            {
                                "path": str(candidate.resolve()),
                                "line": line_number,
                                "content": line,
                            }
                        )
            return matches

        matches = await asyncio.to_thread(search)
        return {
            "path": str(root.resolve()),
            "query": query,
            "file_pattern": file_pattern,
            "matches": matches,
        }
