"""Adapters exposing exact remote MCP tools through the local Tool API."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import hashlib
import json
from typing import Any, Protocol

from mewcode_agent.mcp.models import (
    MAX_MCP_ERROR_MESSAGE_BYTES,
    MAX_MCP_RESULT_BYTES,
    McpError,
    McpServerConfig,
    McpToolCallResult,
    McpToolDefinition,
    thaw_json,
)
from mewcode_agent.mcp.protocol import JsonRpcRemoteError
from mewcode_agent.tools.base import Tool, ToolExecutionError


class McpToolInvoker(Protocol):
    async def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: Mapping[str, Any],
    ) -> McpToolCallResult: ...


def local_tool_name(server_id: str, remote_tool_name: str) -> str:
    """Build the stable, security-compatible alias defined by the spec."""

    if not isinstance(server_id, str) or not server_id:
        raise ValueError("server_id 必须是非空字符串")
    if not isinstance(remote_tool_name, str) or not remote_tool_name:
        raise ValueError("remote_tool_name 必须是非空字符串")
    digest_input = f"{server_id}\0{remote_tool_name}".encode("utf-8")
    digest = hashlib.sha256(digest_input).hexdigest()[:24]
    return f"mcp_{server_id}_{digest}"


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value
    marker = "…"
    budget = limit - len(marker.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + marker


class RemoteMcpTool(Tool):
    """One stable local alias backed by one exact remote MCP tool name."""

    def __init__(
        self,
        config: McpServerConfig,
        definition: McpToolDefinition,
        invoker: McpToolInvoker,
    ) -> None:
        self.name = local_tool_name(config.server_id, definition.name)
        self.server_id = config.server_id
        self.remote_tool_name = definition.name
        self.description = (
            f"MCP server {config.server_id} 的远端工具 {definition.name}: "
            f"{definition.description}"
        )
        self.parameters = thaw_json(definition.input_schema)
        self.category = config.tool_categories.get(definition.name, "command")
        self.timeout_seconds = config.request_timeout_seconds
        self._invoker = invoker

    async def execute(self, arguments: dict[str, Any]) -> Any:
        try:
            result = await self._invoker.call_tool(
                self.server_id,
                self.remote_tool_name,
                arguments,
            )
            normalized = self._normalize_result(result)
            if result.is_error:
                raise ToolExecutionError(
                    "mcp_tool_error",
                    self._tool_error_message(result),
                    details={
                        "content": normalized["content"],
                        "structured_content": normalized[
                            "structured_content"
                        ],
                    },
                )
            return normalized
        except asyncio.CancelledError:
            raise
        except JsonRpcRemoteError as exc:
            raise ToolExecutionError(
                "mcp_protocol_error",
                f"JSON-RPC error {exc.rpc_code}: {exc.remote_message}",
            ) from exc
        except McpError as exc:
            raise ToolExecutionError(exc.code, exc.message) from exc

    def _normalize_result(
        self,
        result: McpToolCallResult,
    ) -> dict[str, Any]:
        normalized = {
            "server_id": self.server_id,
            "remote_tool_name": self.remote_tool_name,
            "content": thaw_json(result.content),
            "structured_content": thaw_json(result.structured_content),
        }
        try:
            encoded = json.dumps(
                normalized,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ToolExecutionError(
                "mcp_invalid_tool_result",
                "MCP 工具结果无法序列化为 JSON",
            ) from exc
        if len(encoded) > MAX_MCP_RESULT_BYTES:
            raise ToolExecutionError(
                "mcp_result_too_large",
                "MCP 工具结果超过 4 MiB 上限",
            )
        return normalized

    @staticmethod
    def _tool_error_message(result: McpToolCallResult) -> str:
        text_parts: list[str] = []
        for item in result.content:
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        message = "\n".join(text_parts) or "MCP 远端工具返回执行错误"
        return _truncate_utf8(message, MAX_MCP_ERROR_MESSAGE_BYTES)
