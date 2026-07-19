"""Shared transport callback and lifecycle contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeAlias

from mewcode_agent.mcp.models import McpError

InboundMessageHandler: TypeAlias = Callable[[bytes | str], Awaitable[None]]
CloseHandler: TypeAlias = Callable[[McpError], None]


class McpTransport(ABC):
    """One reusable bidirectional transport for an MCP session."""

    @abstractmethod
    async def connect(
        self,
        on_message: InboundMessageHandler,
        on_close: CloseHandler,
    ) -> None:
        """Open resources and attach inbound lifecycle callbacks."""

    @abstractmethod
    async def send(self, message: Mapping[str, Any]) -> None:
        """Send one complete JSON-RPC object."""

    @abstractmethod
    def mark_initialized(self, protocol_version: str) -> None:
        """Enable post-initialize transport metadata."""

    async def start_listener(self) -> None:
        """Start an optional server-initiated message listener."""

    async def reset_session(self) -> None:
        """Discard transport state tied to an expired logical session."""

    @abstractmethod
    async def close(self) -> None:
        """Release all transport resources."""
