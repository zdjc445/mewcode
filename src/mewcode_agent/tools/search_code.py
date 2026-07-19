"""Regular-expression code search tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import Any

from mewcode_agent.security.path_sandbox import PathSandbox, PathSandboxError
from mewcode_agent.tools._paths import expand_path
from mewcode_agent.tools.base import Tool, ToolExecutionError, validate_arguments


class SearchCodeTool(Tool):
    category = "read"
    name = "search_code"
    description = (
        "使用 Python 正则表达式搜索 UTF-8 文本文件内容，返回文件、行号和匹配行。"
        "无法按 UTF-8 读取的文件会被跳过。"
        "搜索根目录和所有候选文件都必须位于启动工作目录内。"
        "这是代码内容搜索的专用工具；不要使用 run_command 代替代码搜索。"
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

    def __init__(self, path_sandbox: PathSandbox | None = None) -> None:
        self._path_sandbox = path_sandbox

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_arguments(
            arguments,
            required={"query": str},
            optional={"path": str, "file_pattern": str},
        )
        query = arguments["query"]
        default_root = (
            self._path_sandbox.working_directory
            if self._path_sandbox is not None
            else Path.cwd()
        )
        raw_root = arguments.get("path", str(default_root))
        root = (
            self._path_sandbox.resolve(raw_root)
            if self._path_sandbox is not None
            else expand_path(raw_root)
        )
        file_pattern = arguments.get("file_pattern", "**/*")
        if (
            self._path_sandbox is not None
            and not self._path_sandbox.pattern_is_safe(file_pattern)
        ):
            raise PathSandboxError("文件模式尝试越过路径沙箱")
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
                if self._path_sandbox is not None:
                    candidate = self._path_sandbox.resolve(candidate)
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
