"""Immutable MCP configuration and shared error models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, TypeAlias

from mewcode_agent.tools.base import ToolCategory

MCP_PROTOCOL_VERSION = "2025-11-25"

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
