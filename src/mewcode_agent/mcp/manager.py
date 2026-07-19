"""Session-scoped MCP connection reuse, activation, refresh, and reconnect."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, TypeAlias

from mewcode_agent.mcp.adapter import RemoteMcpTool
from mewcode_agent.mcp.client import McpClient
from mewcode_agent.mcp.models import (
    McpConfiguration,
    McpConnectionLost,
    McpDiagnostic,
    McpError,
    McpServerConfig,
    McpSessionExpired,
    McpToolCallResult,
)
from mewcode_agent.mcp.transports import (
    McpTransport,
    StdioTransport,
    StreamableHttpTransport,
)
from mewcode_agent.tools.registry import ToolRegistry

TransportFactory: TypeAlias = Callable[[McpServerConfig], McpTransport]
DiagnosticHandler: TypeAlias = Callable[[McpDiagnostic], None]


@dataclass(slots=True)
class _RequiredActivationFailure(Exception):
    config: McpServerConfig
    error: Exception


def create_transport(config: McpServerConfig) -> McpTransport:
    if config.transport == "stdio":
        return StdioTransport(config)
    return StreamableHttpTransport(config)


class McpConnectionManager:
    """Own at most one active MCP client per configured server."""

    def __init__(
        self,
        configuration: McpConfiguration,
        registry: ToolRegistry,
        *,
        transport_factory: TransportFactory = create_transport,
        diagnostic_handler: DiagnosticHandler | None = None,
    ) -> None:
        self._configuration = configuration
        self._registry = registry
        self._transport_factory = transport_factory
        self._diagnostic_handler = diagnostic_handler
        self._configs = {
            config.server_id: config for config in configuration.servers
        }
        if len(self._configs) != len(configuration.servers):
            raise ValueError("MCP configuration 包含重复 server ID")
        self._clients: dict[str, McpClient] = {}
        self._reconnect_locks = {
            server_id: asyncio.Lock() for server_id in self._configs
        }
        self._diagnostics: list[McpDiagnostic] = []
        self._activated = False
        self._closed = False

    @property
    def diagnostics(self) -> tuple[McpDiagnostic, ...]:
        return tuple(self._diagnostics)

    @property
    def active_server_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._clients))

    async def activate_all(self) -> None:
        if self._activated or self._closed:
            raise RuntimeError("MCP connection manager 状态无效")
        tasks = {
            asyncio.create_task(
                self._activate_one(config),
                name=f"mcp-activate-{config.server_id}",
            ): config
            for config in self._configuration.servers
        }
        if tasks:
            try:
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_EXCEPTION,
                )
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            required_failed = any(
                not task.cancelled()
                and isinstance(task.exception(), _RequiredActivationFailure)
                for task in done
            )
            if required_failed:
                for task in pending:
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            failures: list[_RequiredActivationFailure] = []
            clients: list[McpClient] = []
            for task in tasks:
                if task.cancelled():
                    continue
                exception = task.exception()
                if isinstance(exception, _RequiredActivationFailure):
                    failures.append(exception)
                    continue
                if exception is not None:
                    raise exception
                client = task.result()
                if client is not None:
                    clients.append(client)
            if failures:
                await asyncio.gather(
                    *(client.close() for client in clients),
                    return_exceptions=True,
                )
                failure = min(failures, key=lambda item: item.config.server_id)
                raise failure.error
            self._clients = {client.server_id: client for client in clients}

        try:
            for server_id in sorted(self._clients):
                client = self._clients[server_id]
                client.set_tools_changed_handler(
                    lambda current_id=server_id: self._refresh_server(current_id)
                )
                self._replace_server_tools(client)
                for diagnostic in client.diagnostics:
                    self._record_diagnostic(diagnostic)
        except Exception:
            for server_id in tuple(self._clients):
                self._registry.replace_mcp_tools(server_id, ())
            await asyncio.gather(
                *(client.close() for client in self._clients.values()),
                return_exceptions=True,
            )
            self._clients.clear()
            raise
        self._activated = True

    async def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: Mapping[str, Any],
    ) -> McpToolCallResult:
        if self._closed:
            raise McpConnectionLost("MCP connection manager 已关闭")
        client = self._clients.get(server_id)
        if client is None:
            raise McpConnectionLost("MCP server 未激活")
        if not client.connected:
            client = await self._reconnect_server(server_id, client)
        try:
            return await client.call_tool(remote_tool_name, arguments)
        except McpSessionExpired:
            client = await self._reinitialize_expired_session(server_id, client)
            try:
                return await client.call_tool(remote_tool_name, arguments)
            except McpSessionExpired as exc:
                raise McpConnectionLost("MCP HTTP session 再次失效") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        clients = tuple(self._clients.values())
        results = await asyncio.gather(
            *(client.close() for client in clients),
            return_exceptions=True,
        )
        for client, result in zip(clients, results, strict=True):
            if isinstance(result, BaseException):
                code = result.code if isinstance(result, McpError) else "mcp_shutdown_failed"
                self._record_diagnostic(
                    McpDiagnostic(
                        server_id=client.server_id,
                        code=code,
                        message="MCP server 关闭时需要额外清理",
                    )
                )
        for server_id in tuple(self._clients):
            self._registry.replace_mcp_tools(server_id, ())
        self._clients.clear()

    async def _activate_one(self, config: McpServerConfig) -> McpClient | None:
        client: McpClient | None = None
        try:
            client = McpClient(config, self._transport_factory(config))
            await client.connect()
            return client
        except asyncio.CancelledError:
            if client is not None:
                with suppress(BaseException):
                    await client.close()
            raise
        except Exception as exc:
            if client is not None:
                with suppress(BaseException):
                    await client.close()
            if config.required:
                raise _RequiredActivationFailure(config, exc) from exc
            code = exc.code if isinstance(exc, McpError) else "mcp_connect_failed"
            self._record_diagnostic(
                McpDiagnostic(
                    server_id=config.server_id,
                    code=code,
                    message="optional MCP server 激活失败，已跳过",
                )
            )
            return None

    async def _refresh_server(self, server_id: str) -> None:
        client = self._clients.get(server_id)
        if client is None or self._closed:
            return
        try:
            await client.discover_tools()
            self._replace_server_tools(client)
            for diagnostic in client.diagnostics:
                self._record_diagnostic(diagnostic)
        except Exception as exc:
            code = exc.code if isinstance(exc, McpError) else "mcp_protocol_error"
            self._record_diagnostic(
                McpDiagnostic(
                    server_id=server_id,
                    code=code,
                    message="MCP 工具列表刷新失败，保留旧快照",
                )
            )

    async def _reconnect_server(
        self,
        server_id: str,
        expected_client: McpClient,
    ) -> McpClient:
        async with self._reconnect_locks[server_id]:
            current = self._clients.get(server_id)
            if current is not None and current is not expected_client and current.connected:
                return current
            if current is not None and current.connected:
                return current
            config = self._configs[server_id]
            replacement = McpClient(config, self._transport_factory(config))
            try:
                await replacement.connect()
                replacement.set_tools_changed_handler(
                    lambda: self._refresh_server(server_id)
                )
                self._replace_server_tools(replacement)
            except BaseException:
                with suppress(BaseException):
                    await replacement.close()
                raise
            self._clients[server_id] = replacement
            if current is not None:
                with suppress(BaseException):
                    await current.close()
            return replacement

    async def _reinitialize_expired_session(
        self,
        server_id: str,
        expected_client: McpClient,
    ) -> McpClient:
        async with self._reconnect_locks[server_id]:
            current = self._clients.get(server_id)
            if current is None:
                raise McpConnectionLost("MCP server 未激活")
            if current is not expected_client:
                return current
            if current.connected:
                return current
            await current.reinitialize()
            self._replace_server_tools(current)
            return current

    def _replace_server_tools(self, client: McpClient) -> None:
        adapters = tuple(
            RemoteMcpTool(client.config, definition, self)
            for definition in client.tools
        )
        aliases = [adapter.name for adapter in adapters]
        if len(aliases) != len(set(aliases)):
            raise ValueError("MCP 工具稳定别名发生冲突")
        self._registry.replace_mcp_tools(client.server_id, adapters)

    def _record_diagnostic(self, diagnostic: McpDiagnostic) -> None:
        self._diagnostics.append(diagnostic)
        if self._diagnostic_handler is not None:
            self._diagnostic_handler(diagnostic)
