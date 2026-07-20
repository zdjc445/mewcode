"""Read-only access to tool-result artifacts from the current session."""

from __future__ import annotations

from typing import Any

from mewcode_agent.compaction.artifacts import ContextArtifactStore
from mewcode_agent.compaction.models import ContextCompactionError
from mewcode_agent.tools.base import Tool, ToolExecutionError, validate_arguments


MAX_ARTIFACT_READ_CHARACTERS = 8192


class ReadContextArtifactTool(Tool):
    category = "read"
    name = "read_context_artifact"
    description = (
        "分页读取当前 MewCode 会话外置的完整工具结果。"
        "只能读取工具结果预览中给出的精确 artifact 绝对路径，"
        "不能用于读取项目文件、其他会话或任意用户目录文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "工具结果预览返回的精确 artifact 绝对路径",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "开始读取的 Unicode 字符偏移量",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_ARTIFACT_READ_CHARACTERS,
                "default": MAX_ARTIFACT_READ_CHARACTERS,
                "description": "最多返回的 Unicode 字符数",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, store: ContextArtifactStore) -> None:
        self._store = store

    async def execute(self, arguments: dict[str, Any]) -> dict[str, object]:
        validate_arguments(
            arguments,
            required={"path": str},
            optional={"offset": int, "limit": int},
        )
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", MAX_ARTIFACT_READ_CHARACTERS)
        if type(offset) is not int or offset < 0:
            raise ToolExecutionError(
                "invalid_arguments",
                "参数 offset 必须是大于或等于 0 的整数",
            )
        if (
            type(limit) is not int
            or not 1 <= limit <= MAX_ARTIFACT_READ_CHARACTERS
        ):
            raise ToolExecutionError(
                "invalid_arguments",
                (
                    "参数 limit 必须是 1 到 "
                    f"{MAX_ARTIFACT_READ_CHARACTERS} 之间的整数"
                ),
            )
        try:
            return await self._store.read(
                arguments["path"],
                offset=offset,
                limit=limit,
            )
        except ContextCompactionError as exc:
            raise ToolExecutionError(exc.code, exc.message) from exc
