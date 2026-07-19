"""Immutable MCP configuration and shared error models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, TypeAlias

from mewcode_agent.tools.base import ToolCategory

MCP_PROTOCOL_VERSION = "2025-11-25"
MAX_MCP_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_MCP_ERROR_MESSAGE_BYTES = 2 * 1024

McpTransportName: TypeAlias = Literal["stdio", "streamable_http"]


class McpError(RuntimeError):
    """An MCP failure with a stable code and safe user-facing message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class McpConfigError(McpError):
    """A strict MCP configuration error."""

    def __init__(self, message: str) -> None:
        super().__init__("mcp_config_error", message)


class McpProtocolError(McpError):
    """A JSON-RPC or MCP protocol violation."""

    def __init__(self, message: str) -> None:
        super().__init__("mcp_protocol_error", message)


class McpRequestTimeout(McpError):
    """A lifecycle request exceeded its configured timeout."""

    def __init__(self, message: str) -> None:
        super().__init__("mcp_request_timeout", message)


class McpConnectionLost(McpError):
    """The transport closed while requests were pending."""

    def __init__(self, message: str = "MCP 连接已关闭") -> None:
        super().__init__("mcp_connection_lost", message)


class McpMessageTooLarge(McpError):
    """An inbound or outbound protocol message exceeded the fixed limit."""

    def __init__(self) -> None:
        super().__init__("mcp_message_too_large", "MCP 消息超过 8 MiB 上限")


def _freeze_mapping(value: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(value))


def _freeze_categories(
    value: Mapping[str, ToolCategory],
) -> Mapping[str, ToolCategory]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class StdioServerConfig:
    """Resolved configuration for one enabled stdio MCP server."""

    server_id: str
    required: bool
    command: str
    args: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(repr=False)
    connect_timeout_seconds: float
    request_timeout_seconds: float
    shutdown_timeout_seconds: float
    tool_categories: Mapping[str, ToolCategory]
    transport: Literal["stdio"] = field(default="stdio", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", _freeze_mapping(self.env))
        object.__setattr__(
            self,
            "tool_categories",
            _freeze_categories(self.tool_categories),
        )


@dataclass(frozen=True, slots=True)
class StreamableHttpServerConfig:
    """Resolved configuration for one enabled Streamable HTTP server."""

    server_id: str
    required: bool
    url: str
    headers: Mapping[str, str] = field(repr=False)
    connect_timeout_seconds: float
    request_timeout_seconds: float
    shutdown_timeout_seconds: float
    tool_categories: Mapping[str, ToolCategory]
    transport: Literal["streamable_http"] = field(
        default="streamable_http",
        init=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _freeze_mapping(self.headers))
        object.__setattr__(
            self,
            "tool_categories",
            _freeze_categories(self.tool_categories),
        )


McpServerConfig: TypeAlias = StdioServerConfig | StreamableHttpServerConfig


@dataclass(frozen=True, slots=True)
class McpConfiguration:
    """All enabled MCP servers for one application session."""

    servers: tuple[McpServerConfig, ...] = ()
