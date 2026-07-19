"""UTF-8 file writing tool."""

from __future__ import annotations

import asyncio
from typing import Any

from mewcode_agent.security.path_sandbox import PathSandbox
from mewcode_agent.tools._paths import expand_path
from mewcode_agent.tools.base import Tool, validate_arguments
from mewcode_agent.tools.file_state_cache import FileStateCache


class WriteFileTool(Tool):
    category = "write"
    name = "write_file"
    description = (
        "以 UTF-8 写入完整文件内容，覆盖已有文件；父目录不存在时自动创建。"
        "已有文件必须先通过 read_file 读取且读取后未被修改；新文件可直接创建。"
        "path 可以是相对路径或绝对路径，但规范化结果必须位于启动工作目录内。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要写入的文件路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        file_state_cache: FileStateCache,
        path_sandbox: PathSandbox | None = None,
    ) -> None:
        self._file_state_cache = file_state_cache
        self._path_sandbox = path_sandbox

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_arguments(
            arguments,
            required={"path": str, "content": str},
        )
        path = (
            self._path_sandbox.resolve(arguments["path"])
            if self._path_sandbox is not None
            else expand_path(arguments["path"])
        )
        content = arguments["content"]

        def write() -> None:
            if path.exists():
                self._file_state_cache.ensure_current(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self._file_state_cache.record(path)

        await asyncio.to_thread(write)
        return {
            "path": str(path.resolve()),
            "characters_written": len(content),
        }
