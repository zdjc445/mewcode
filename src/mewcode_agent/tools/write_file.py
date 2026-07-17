"""UTF-8 file writing tool."""

from __future__ import annotations

import asyncio
from typing import Any

from mewcode_agent.tools._paths import expand_path
from mewcode_agent.tools.base import Tool, validate_arguments


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "以 UTF-8 写入完整文件内容，覆盖已有文件；父目录不存在时自动创建。"
        "path 可以是相对路径或绝对路径。"
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

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_arguments(
            arguments,
            required={"path": str, "content": str},
        )
        path = expand_path(arguments["path"])
        content = arguments["content"]

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(write)
        return {
            "path": str(path.resolve()),
            "characters_written": len(content),
        }

