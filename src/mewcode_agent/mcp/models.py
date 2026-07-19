"""Immutable MCP configuration and shared error models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypeAlias

from mewcode_agent.tools.base import ToolCategory

MCP_PROTOCOL_VERSION = "2025-11-25"
MAX_MCP_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_MCP_ERROR_MESSAGE_BYTES = 2 * 1024
MAX_MCP_STDERR_BYTES = 256 * 1024

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


class McpConnectFailed(McpError):
    """An MCP transport could not be established."""

    def __init__(self, message: str) -> None:
        super().__init__("mcp_connect_failed", message)


class McpShutdownFailed(McpError):
    """An MCP transport required forced or incomplete shutdown."""

    def __init__(self, message: str) -> None:
        super().__init__("mcp_shutdown_failed", message)


class McpSessionExpired(McpError):
    """A Streamable HTTP server explicitly rejected an old session."""

    def __init__(self, message: str = "MCP HTTP session 已失效") -> None:
        super().__init__("mcp_session_expired", message)


class UnsupportedMcpVersion(McpError):
    """The server did not negotiate the sole supported MCP version."""

    def __init__(self) -> None:
        super().__init__(
            "unsupported_mcp_version",
            f"MCP server 未接受协议版本 {MCP_PROTOCOL_VERSION}",
        )


class McpToolsCapabilityMissing(McpError):
    """The server did not advertise the required Tools capability."""

    def __init__(self) -> None:
        super().__init__(
            "mcp_tools_capability_missing",
            "MCP server 未声明 Tools capability",
        )


class McpToolNotFound(McpError):
    """A discovered remote tool no longer exists."""

    def __init__(self) -> None:
        super().__init__("mcp_tool_not_found", "MCP 远端工具不存在")


class McpInvalidToolResult(McpError):
    """A tools/call result violated the MCP or declared output schema."""

    def __init__(self, message: str) -> None:
        super().__init__("mcp_invalid_tool_result", message)


def freeze_json(value: Any) -> Any:
    """Recursively freeze JSON-compatible containers."""

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object 的字段名必须是字符串")
        return MappingProxyType(
            {key: freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    """Return mutable JSON-compatible containers from a frozen snapshot."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class McpServerInfo:
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class McpToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None = None
    annotations: Mapping[str, Any] = field(default_factory=dict)
    task_support: Literal["forbidden", "optional"] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", freeze_json(self.input_schema))
        if self.output_schema is not None:
            object.__setattr__(
                self,
                "output_schema",
                freeze_json(self.output_schema),
            )
        object.__setattr__(self, "annotations", freeze_json(self.annotations))


@dataclass(frozen=True, slots=True)
class McpDiagnostic:
    server_id: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class McpServerSnapshot:
    server_id: str
    server_info: McpServerInfo
    capabilities: Mapping[str, Any]
    instructions: str | None
    tools: tuple[McpToolDefinition, ...]
    list_changed: bool
    diagnostics: tuple[McpDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", freeze_json(self.capabilities))


@dataclass(frozen=True, slots=True)
class McpToolCallResult:
    content: tuple[Mapping[str, Any], ...]
    structured_content: Mapping[str, Any] | None
    is_error: bool
    meta: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content",
            tuple(freeze_json(item) for item in self.content),
        )
        if self.structured_content is not None:
            object.__setattr__(
                self,
                "structured_content",
                freeze_json(self.structured_content),
            )
        object.__setattr__(self, "meta", freeze_json(self.meta))


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
