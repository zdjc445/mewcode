"""Glob-based file discovery tool."""

from __future__ import annotations

import asyncio
import glob
from pathlib import Path
from typing import Any

from mewcode_agent.security.path_sandbox import PathSandbox, PathSandboxError
from mewcode_agent.tools._paths import expand_path
from mewcode_agent.tools.base import Tool, ToolExecutionError, validate_arguments


class FindFilesTool(Tool):
    category = "read"
    name = "find_files"
    description = (
        "按 glob 模式查找文件并返回绝对路径列表。支持 ** 递归模式，并包含隐藏文件。"
        "搜索根目录和所有匹配结果都必须位于启动工作目录内。"
        "这是文件发现的专用工具；不要使用 run_command 代替文件查找。"
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

    def __init__(self, path_sandbox: PathSandbox | None = None) -> None:
        self._path_sandbox = path_sandbox

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
        if (
            self._path_sandbox is not None
            and not self._path_sandbox.pattern_is_safe(pattern)
        ):
            raise PathSandboxError("文件模式尝试越过路径沙箱")

        def find() -> list[str]:
            matches = glob.glob(
                str(root / pattern),
                recursive=True,
                include_hidden=True,
            )
            resolved_matches: list[str] = []
            for match in matches:
                candidate = Path(match)
                if not candidate.is_file():
                    continue
                resolved = (
                    self._path_sandbox.resolve(candidate)
                    if self._path_sandbox is not None
                    else candidate.resolve()
                )
                resolved_matches.append(str(resolved))
            return sorted(resolved_matches)

        matches = await asyncio.to_thread(find)
        return {
            "path": str(root.resolve()),
            "pattern": pattern,
            "matches": matches,
        }
