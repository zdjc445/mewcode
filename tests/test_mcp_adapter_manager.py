from __future__ import annotations

import asyncio
from collections.abc import Mapping
import hashlib
import json
from typing import Any

import pytest

from mewcode_agent.mcp import (
    MAX_MCP_ERROR_MESSAGE_BYTES,
    MAX_MCP_RESULT_BYTES,
    MCP_PROTOCOL_VERSION,
    JsonRpcRemoteError,
    McpConfiguration,
    McpConnectionLost,
    McpConnectionManager,
    McpSessionExpired,
    McpToolCallResult,
    McpToolDefinition,
    RemoteMcpTool,
    StreamableHttpServerConfig,
    UnsupportedMcpVersion,
    local_tool_name,
)
from mewcode_agent.mcp.transports.base import (
    CloseHandler,
    InboundMessageHandler,
    McpTransport,
)
from mewcode_agent.tools import Tool, ToolExecutionError, ToolRegistry


def _config(
    server_id: str,
    *,
    required: bool = True,
    categories: Mapping[str, str] | None = None,
) -> StreamableHttpServerConfig:
    return StreamableHttpServerConfig(
        server_id=server_id,
        required=required,
        url=f"https://{server_id}.example.test/mcp",
        headers={},
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        shutdown_timeout_seconds=1,
        tool_categories=categories or {},  # type: ignore[arg-type]
    )


def _definition(name: str = "Remote.Tool") -> McpToolDefinition:
    return McpToolDefinition(
        name=name,
        description="remote description",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
        },
        annotations={"readOnlyHint": True},
    )


class StubInvoker:
    def __init__(self, result: McpToolCallResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[str, str, Mapping[str, Any]]] = []

    async def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: Mapping[str, Any],
    ) -> McpToolCallResult:
        self.calls.append((server_id, remote_tool_name, arguments))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_local_alias_uses_exact_sha256_formula_and_preserves_case() -> None:
    expected = hashlib.sha256(b"server_a\0Case.Tool-name").hexdigest()[:24]

    assert local_tool_name("server_a", "Case.Tool-name") == (
        f"mcp_server_a_{expected}"
    )
    assert local_tool_name("server_a", "Case.Tool-name") != local_tool_name(
        "server_a",
        "case.tool-name",
    )


def test_remote_adapter_fields_default_category_and_exact_override() -> None:
    definition = _definition()
    default_tool = RemoteMcpTool(
        _config("server_a"),
        definition,
        StubInvoker(McpToolCallResult((), None, False)),
    )
    read_tool = RemoteMcpTool(
        _config("server_a", categories={"Remote.Tool": "read"}),
        definition,
        StubInvoker(McpToolCallResult((), None, False)),
    )

    assert default_tool.category == "command"
    assert read_tool.category == "read"
    assert read_tool.timeout_seconds == 1
    assert read_tool.remote_tool_name == "Remote.Tool"
    assert "server_a" in read_tool.description
    assert "Remote.Tool" in read_tool.description
    assert read_tool.parameters == {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
    }


async def test_remote_adapter_normalizes_success_and_drops_meta() -> None:
    invoker = StubInvoker(
        McpToolCallResult(
            content=({"type": "text", "text": "ok"},),
            structured_content={"value": 2},
            is_error=False,
            meta={"secret": "must-not-leak"},
        )
    )
    tool = RemoteMcpTool(_config("server_a"), _definition(), invoker)

    result = await tool.execute({"value": 2})

    assert result == {
        "server_id": "server_a",
        "remote_tool_name": "Remote.Tool",
        "content": [{"type": "text", "text": "ok"}],
        "structured_content": {"value": 2},
    }
    assert "must-not-leak" not in json.dumps(result)
    assert invoker.calls == [("server_a", "Remote.Tool", {"value": 2})]


async def test_remote_adapter_maps_tool_error_and_truncates_text() -> None:
    long_message = "错" * MAX_MCP_ERROR_MESSAGE_BYTES
    tool = RemoteMcpTool(
        _config("server_a"),
        _definition(),
        StubInvoker(
            McpToolCallResult(
                content=({"type": "text", "text": long_message},),
                structured_content={"reason": "bad"},
                is_error=True,
            )
        ),
    )

    with pytest.raises(ToolExecutionError) as caught:
        await tool.execute({})

    assert caught.value.code == "mcp_tool_error"
    assert len(caught.value.message.encode("utf-8")) <= (
        MAX_MCP_ERROR_MESSAGE_BYTES
    )
    assert caught.value.details == {
        "content": [{"type": "text", "text": long_message}],
        "structured_content": {"reason": "bad"},
    }


async def test_remote_adapter_hides_json_rpc_error_data() -> None:
    tool = RemoteMcpTool(
        _config("server_a"),
        _definition(),
        StubInvoker(
            JsonRpcRemoteError(
                -32000,
                "remote failure",
                data={"secret": "hidden-data"},
            )
        ),
    )

    with pytest.raises(ToolExecutionError) as caught:
        await tool.execute({})

    assert caught.value.code == "mcp_protocol_error"
    assert "-32000" in caught.value.message
    assert "hidden-data" not in caught.value.message
    assert caught.value.details is None


async def test_remote_adapter_enforces_normalized_result_limit() -> None:
    tool = RemoteMcpTool(
        _config("server_a"),
        _definition(),
        StubInvoker(
            McpToolCallResult(
                content=(
                    {
                        "type": "text",
                        "text": "x" * MAX_MCP_RESULT_BYTES,
                    },
                ),
                structured_content=None,
                is_error=False,
            )
        ),
    )

    with pytest.raises(ToolExecutionError) as caught:
        await tool.execute({})

    assert caught.value.code == "mcp_result_too_large"


class BaseTool(Tool):
    name = "base_tool"
    description = "base"
    parameters = {"type": "object", "properties": {}}
    category = "read"

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {}


def test_registry_replaces_one_mcp_group_atomically_and_keeps_stable_order() -> None:
    registry = ToolRegistry()
    registry.register(BaseTool())
    invoker = StubInvoker(McpToolCallResult((), None, False))
    b_tool = RemoteMcpTool(_config("server_b"), _definition("z"), invoker)
    a_second = RemoteMcpTool(_config("server_a"), _definition("z"), invoker)
    a_first = RemoteMcpTool(_config("server_a"), _definition("A"), invoker)

    registry.replace_mcp_tools("server_b", (b_tool,))
    registry.replace_mcp_tools("server_a", (a_first, a_second))

    assert [item["function"]["name"] for item in registry.api_tools("openai")] == [
        "base_tool",
        a_first.name,
        a_second.name,
        b_tool.name,
    ]
    with pytest.raises(ValueError, match="冲突"):
        registry.replace_mcp_tools("server_a", (a_first, a_first))
    assert registry.get(a_first.name) is a_first
    registry.replace_mcp_tools("server_a", ())
    assert registry.get(a_first.name) is None
    assert registry.get(b_tool.name) is b_tool


def test_registry_reserves_mcp_prefix_for_managed_groups() -> None:
    class InvalidExtensionTool(BaseTool):
        name = "mcp_unmanaged"

    registry = ToolRegistry()
    with pytest.raises(ValueError, match="保留"):
        registry.register(InvalidExtensionTool())


class ManagedTransport(McpTransport):
    def __init__(
        self,
        server_id: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        list_changed: bool = False,
        unsupported_version: bool = False,
        initialize_gate: asyncio.Event | None = None,
        expire_calls: int = 0,
        lose_calls: int = 0,
        expire_gate: asyncio.Event | None = None,
    ) -> None:
        self.server_id = server_id
        self.tools: Any = tools if tools is not None else [_managed_tool("remote")]
        self.list_changed = list_changed
        self.unsupported_version = unsupported_version
        self.initialize_gate = initialize_gate
        self.expire_calls = expire_calls
        self.lose_calls = lose_calls
        self.expire_gate = expire_gate
        self.initialize_started = asyncio.Event()
        self.initialize_count = 0
        self.call_count = 0
        self.reset_count = 0
        self.expire_list_calls = 0
        self.closed = False
        self._on_message: InboundMessageHandler | None = None
        self._on_close: CloseHandler | None = None

    async def connect(
        self,
        on_message: InboundMessageHandler,
        on_close: CloseHandler,
    ) -> None:
        self._on_message = on_message
        self._on_close = on_close

    async def send(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return
        if method == "initialize":
            self.initialize_count += 1
            self.initialize_started.set()
            if self.initialize_gate is not None:
                await self.initialize_gate.wait()
            result = {
                "protocolVersion": (
                    "2025-06-18"
                    if self.unsupported_version
                    else MCP_PROTOCOL_VERSION
                ),
                "capabilities": {
                    "tools": {"listChanged": self.list_changed}
                },
                "serverInfo": {"name": self.server_id, "version": "1"},
            }
        elif method == "tools/list":
            if self.expire_list_calls:
                self.expire_list_calls -= 1
                raise McpSessionExpired()
            result = {"tools": self.tools}
        elif method == "tools/call":
            self.call_count += 1
            if self.expire_calls:
                self.expire_calls -= 1
                if self.expire_gate is not None:
                    await self.expire_gate.wait()
                raise McpSessionExpired()
            if self.lose_calls:
                self.lose_calls -= 1
                error = McpConnectionLost("simulated lost response")
                assert self._on_close is not None
                self._on_close(error)
                raise error
            result = {
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"server": self.server_id},
            }
        else:
            raise AssertionError(f"unexpected method: {method}")
        await self._emit(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        )

    def mark_initialized(self, protocol_version: str) -> None:
        assert protocol_version == MCP_PROTOCOL_VERSION

    async def reset_session(self) -> None:
        self.reset_count += 1

    async def close(self) -> None:
        self.closed = True

    async def emit_list_changed(self) -> None:
        await self._emit(
            {
                "jsonrpc": "2.0",
                "method": "notifications/tools/list_changed",
            }
        )

    async def _emit(self, message: Mapping[str, Any]) -> None:
        assert self._on_message is not None
        await self._on_message(json.dumps(message))


def _managed_tool(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"tool {name}",
        "inputSchema": {"type": "object"},
        "outputSchema": {
            "type": "object",
            "properties": {"server": {"type": "string"}},
            "required": ["server"],
        },
    }


class SequenceFactory:
    def __init__(self, transports: Mapping[str, list[ManagedTransport]]) -> None:
        self.transports = {key: list(value) for key, value in transports.items()}
        self.created: list[ManagedTransport] = []

    def __call__(self, config: Any) -> ManagedTransport:
        transport = self.transports[config.server_id].pop(0)
        self.created.append(transport)
        return transport


async def _wait_until(predicate: Any, *, timeout: float = 1) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


async def test_manager_activates_concurrently_and_registers_stable_order() -> None:
    registry = ToolRegistry()
    registry.register(BaseTool())
    a = ManagedTransport(
        "server_a",
        tools=[_managed_tool("z"), _managed_tool("A")],
    )
    b = ManagedTransport("server_b", tools=[_managed_tool("remote")])
    manager = McpConnectionManager(
        McpConfiguration((_config("server_b"), _config("server_a"))),
        registry,
        transport_factory=SequenceFactory({"server_a": [a], "server_b": [b]}),
    )

    await manager.activate_all()

    assert manager.active_server_ids == ("server_a", "server_b")
    assert [item["function"]["name"] for item in registry.api_tools("openai")] == [
        "base_tool",
        local_tool_name("server_a", "A"),
        local_tool_name("server_a", "z"),
        local_tool_name("server_b", "remote"),
    ]
    await manager.close()
    assert [item["function"]["name"] for item in registry.api_tools("openai")] == [
        "base_tool"
    ]


async def test_required_failure_cancels_and_closes_other_activation() -> None:
    failure_gate = asyncio.Event()
    slow = ManagedTransport("slow_optional", initialize_gate=asyncio.Event())
    bad = ManagedTransport(
        "required_bad",
        unsupported_version=True,
        initialize_gate=failure_gate,
    )
    manager = McpConnectionManager(
        McpConfiguration(
            (
                _config("slow_optional", required=False),
                _config("required_bad", required=True),
            )
        ),
        ToolRegistry(),
        transport_factory=SequenceFactory(
            {"slow_optional": [slow], "required_bad": [bad]}
        ),
    )
    activation = asyncio.create_task(manager.activate_all())
    await asyncio.wait_for(slow.initialize_started.wait(), timeout=1)
    await asyncio.wait_for(bad.initialize_started.wait(), timeout=1)
    failure_gate.set()

    with pytest.raises(UnsupportedMcpVersion):
        await activation

    assert slow.closed is True
    assert bad.closed is True


async def test_optional_failure_is_skipped_with_safe_diagnostic() -> None:
    good = ManagedTransport("good")
    bad = ManagedTransport("optional_bad", unsupported_version=True)
    diagnostics: list[Any] = []
    manager = McpConnectionManager(
        McpConfiguration(
            (
                _config("optional_bad", required=False),
                _config("good"),
            )
        ),
        ToolRegistry(),
        transport_factory=SequenceFactory(
            {"optional_bad": [bad], "good": [good]}
        ),
        diagnostic_handler=diagnostics.append,
    )

    await manager.activate_all()

    assert manager.active_server_ids == ("good",)
    assert diagnostics[0].server_id == "optional_bad"
    assert diagnostics[0].code == "unsupported_mcp_version"
    assert "2025-06-18" not in diagnostics[0].message
    await manager.close()


async def test_manager_reuses_client_for_multiple_tool_calls() -> None:
    transport = ManagedTransport("cached")
    registry = ToolRegistry()
    manager = McpConnectionManager(
        McpConfiguration((_config("cached"),)),
        registry,
        transport_factory=SequenceFactory({"cached": [transport]}),
    )
    await manager.activate_all()
    alias = local_tool_name("cached", "remote")

    first = await registry.execute(alias, "{}")
    second = await registry.execute(alias, "{}")

    assert first.success is True
    assert second.success is True
    assert transport.initialize_count == 1
    assert transport.call_count == 2
    await manager.close()


async def test_expired_http_session_reinitializes_and_retries_once() -> None:
    transport = ManagedTransport("session", expire_calls=1)
    registry = ToolRegistry()
    manager = McpConnectionManager(
        McpConfiguration((_config("session"),)),
        registry,
        transport_factory=SequenceFactory({"session": [transport]}),
    )
    await manager.activate_all()

    result = await registry.execute(local_tool_name("session", "remote"), "{}")

    assert result.success is True
    assert transport.call_count == 2
    assert transport.initialize_count == 2
    assert transport.reset_count == 1
    await manager.close()


async def test_concurrent_expired_calls_share_one_reinitialization() -> None:
    expire_gate = asyncio.Event()
    transport = ManagedTransport(
        "session_concurrent",
        expire_calls=2,
        expire_gate=expire_gate,
    )
    registry = ToolRegistry()
    manager = McpConnectionManager(
        McpConfiguration((_config("session_concurrent"),)),
        registry,
        transport_factory=SequenceFactory(
            {"session_concurrent": [transport]}
        ),
    )
    await manager.activate_all()
    alias = local_tool_name("session_concurrent", "remote")
    first = asyncio.create_task(registry.execute(alias, "{}"))
    second = asyncio.create_task(registry.execute(alias, "{}"))
    await _wait_until(lambda: transport.call_count == 2)
    expire_gate.set()

    results = await asyncio.gather(first, second)

    assert all(result.success for result in results)
    assert transport.call_count == 4
    assert transport.initialize_count == 2
    assert transport.reset_count == 1
    await manager.close()


async def test_ambiguous_connection_loss_is_not_retried_until_next_call() -> None:
    first_transport = ManagedTransport("reconnect", lose_calls=1)
    second_transport = ManagedTransport("reconnect")
    registry = ToolRegistry()
    manager = McpConnectionManager(
        McpConfiguration((_config("reconnect"),)),
        registry,
        transport_factory=SequenceFactory(
            {"reconnect": [first_transport, second_transport]}
        ),
    )
    await manager.activate_all()
    alias = local_tool_name("reconnect", "remote")

    first = await registry.execute(alias, "{}")
    second = await registry.execute(alias, "{}")

    assert first.success is False
    assert first.error_code == "mcp_connection_lost"
    assert first_transport.call_count == 1
    assert second.success is True
    assert second_transport.call_count == 1
    await manager.close()


async def test_list_changed_replaces_only_its_server_snapshot() -> None:
    transport = ManagedTransport(
        "refresh",
        tools=[_managed_tool("old")],
        list_changed=True,
    )
    registry = ToolRegistry()
    manager = McpConnectionManager(
        McpConfiguration((_config("refresh"),)),
        registry,
        transport_factory=SequenceFactory({"refresh": [transport]}),
    )
    await manager.activate_all()
    old_alias = local_tool_name("refresh", "old")
    new_alias = local_tool_name("refresh", "new")
    transport.tools = [_managed_tool("new")]

    await transport.emit_list_changed()
    await _wait_until(lambda: registry.get(new_alias) is not None)

    assert registry.get(old_alias) is None
    assert registry.get(new_alias) is not None
    await manager.close()


async def test_failed_list_changed_refresh_keeps_old_snapshot() -> None:
    transport = ManagedTransport(
        "refresh_fail",
        tools=[_managed_tool("old")],
        list_changed=True,
    )
    registry = ToolRegistry()
    manager = McpConnectionManager(
        McpConfiguration((_config("refresh_fail"),)),
        registry,
        transport_factory=SequenceFactory({"refresh_fail": [transport]}),
    )
    await manager.activate_all()
    old_alias = local_tool_name("refresh_fail", "old")
    transport.tools = "invalid"

    await transport.emit_list_changed()
    await _wait_until(lambda: bool(manager.diagnostics))

    assert registry.get(old_alias) is not None
    assert manager.diagnostics[-1].message == (
        "MCP 工具列表刷新失败，保留旧快照"
    )
    await manager.close()


async def test_list_changed_session_404_reinitializes_and_updates_snapshot() -> None:
    transport = ManagedTransport(
        "refresh_session",
        tools=[_managed_tool("old")],
        list_changed=True,
    )
    registry = ToolRegistry()
    manager = McpConnectionManager(
        McpConfiguration((_config("refresh_session"),)),
        registry,
        transport_factory=SequenceFactory({"refresh_session": [transport]}),
    )
    await manager.activate_all()
    transport.tools = [_managed_tool("new")]
    transport.expire_list_calls = 1
    new_alias = local_tool_name("refresh_session", "new")

    await transport.emit_list_changed()
    await _wait_until(lambda: registry.get(new_alias) is not None)

    assert transport.reset_count == 1
    assert transport.initialize_count == 2
    assert registry.get(local_tool_name("refresh_session", "old")) is None
    await manager.close()
