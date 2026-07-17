"""UTF-8 file reading tool."""

from __future__ import annotations

import asyncio
from typing import Any

from mewcode_agent.tools._paths import expand_path
from mewcode_agent.tools.base import Tool, ToolExecutionError, validate_arguments
from mewcode_agent.tools.file_state_cache import FileStateCache


MAX_READ_LINES = 2000


class ReadFileTool(Tool):
    category = "read"
    name = "read_file"
    description = (
        "按行读取 UTF-8 文本文件，支持使用 offset 和 limit 分页。"
        "成功读取后记录文件状态，供 write_file 和 edit_file 做修改前校验。"
        "path 可以是相对路径或绝对路径。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件路径",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "开始读取的行偏移量，从 0 开始",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_READ_LINES,
                "default": MAX_READ_LINES,
                "description": f"最多返回的行数，不能超过 {MAX_READ_LINES}",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, file_state_cache: FileStateCache) -> None:
        self._file_state_cache = file_state_cache

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_arguments(
            arguments,
            required={"path": str},
            optional={"offset": int, "limit": int},
        )
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", MAX_READ_LINES)
        if isinstance(offset, bool) or offset < 0:
            raise ToolExecutionError(
                "invalid_arguments",
                "参数 offset 必须是大于或等于 0 的整数",
            )
        if isinstance(limit, bool) or not 1 <= limit <= MAX_READ_LINES:
            raise ToolExecutionError(
                "invalid_arguments",
                f"参数 limit 必须是 1 到 {MAX_READ_LINES} 之间的整数",
            )

        path = expand_path(arguments["path"])

        def read() -> str:
            content = path.read_text(encoding="utf-8")
            self._file_state_cache.record(path)
            return content

        content = await asyncio.to_thread(read)
        lines = content.splitlines(keepends=True)
        selected = lines[offset : offset + limit]
        next_offset = offset + len(selected)
        return {
            "path": str(path.resolve()),
            "content": "".join(selected),
            "offset": offset,
            "limit": limit,
            "total_lines": len(lines),
            "has_more": next_offset < len(lines),
            "next_offset": next_offset,
        }
