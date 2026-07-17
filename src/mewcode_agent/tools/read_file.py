"""UTF-8 file reading tool."""

from __future__ import annotations

import asyncio
from typing import Any

from mewcode_agent.tools._paths import expand_path
from mewcode_agent.tools.base import Tool, validate_arguments


class ReadFileTool(Tool):
    name = "read_file"
    description = "读取 UTF-8 文本文件的完整内容。path 可以是相对路径或绝对路径。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string", 
                "description": "要读取的文件路径"
                },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, Any]) -> dict[str, str]:
        validate_arguments(arguments, required={"path": str})
        path = expand_path(arguments["path"])
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return {"path": str(path.resolve()), "content": content}

