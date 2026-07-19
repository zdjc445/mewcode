from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from importlib import metadata
import json
from typing import Any

import pytest

from mewcode_agent.mcp import (
    MCP_PROTOCOL_VERSION,
    McpClient,
    McpInvalidToolResult,
    McpProtocolError,
    McpSessionExpired,
    McpToolNotFound,
    McpToolsCapabilityMissing,
    StreamableHttpServerConfig,
    UnsupportedMcpVersion,
)
from mewcode_agent.mcp.models import McpError
from mewcode_agent.mcp.transports.base import (
    CloseHandler,
    InboundMessageHandler,
    McpTransport,
)


def _tool(
    name: str,
    *,
    description: str | None = "A remote tool",
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    task_support: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "inputSchema": input_schema or {"type": "object"},
        "annotations": {"readOnlyHint": True},
    }
    if description is not None:
        result["description"] = description
    if output_schema is not None:
        result["outputSchema"] = output_schema
    if task_support is not None:
        result["execution"] = {"taskSupport": task_support}
    return result


def _config(
    *,
    tool_categories: Mapping[str, str] | None = None,
) -> StreamableHttpServerConfig:
    return StreamableHttpServerConfig(
        server_id="fake_server",
        required=True,
        url="https://mcp.example.test/endpoint",
        headers={},
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        shutdown_timeout_seconds=1,
        tool_categories=tool_categories or {},  # type: ignore[arg-type]
    )


class FakeTransport(McpTransport):
    def __init__(self) -> None:
        self.initialize_result: dict[str, Any] = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fake", "version": "1.0.0"},
            "instructions": "untrusted server instructions",
        }
        self.pages: dict[str | None, dict[str, Any]] = {
            None: {"tools": [_tool("echo")]}
        }
        self.list_handler: Callable[[str | None], dict[str, Any]] | None = None
        self.call_result: dict[str, Any] = {
            "content": [{"type": "text", "text": "ok"}]
        }
        self.expire_list_calls = 0
        self.messages: list[dict[str, Any]] = []
        self.marked_version: str | None = None
        self.listener_started = False
        self.closed = False
        self.reset_count = 0
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
        copied = json.loads(json.dumps(dict(message)))
        self.messages.append(copied)
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return
        if method == "initialize":
            result = self.initialize_result
        elif method == "tools/list":
            if self.expire_list_calls:
                self.expire_list_calls -= 1
                raise McpSessionExpired()
            params = message.get("params", {})
            cursor = params.get("cursor")
            result = (
                self.list_handler(cursor)
                if self.list_handler is not None
                else self.pages[cursor]
            )
        elif method == "tools/call":
            result = self.call_result
        else:
            raise AssertionError(f"unexpected method: {method}")
        await self._emit(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        )

    def mark_initialized(self, protocol_version: str) -> None:
        self.marked_version = protocol_version

    async def start_listener(self) -> None:
        self.listener_started = True

    async def reset_session(self) -> None:
        self.reset_count += 1
        self.listener_started = False

    async def close(self) -> None:
        self.closed = True

    async def emit_notification(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._emit(message)

    async def _emit(self, message: Mapping[str, Any]) -> None:
        assert self._on_message is not None
        await self._on_message(json.dumps(message))


async def test_client_runs_exact_initialize_initialized_and_discovery_order() -> None:
    transport = FakeTransport()
    transport.initialize_result["capabilities"]["tools"]["listChanged"] = True
    client = McpClient(_config(), transport)

    snapshot = await client.connect()

    assert [message.get("method") for message in transport.messages] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    initialize = transport.messages[0]
    assert initialize["id"] == 1
    assert initialize["params"] == {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {
            "name": "mewcode-agent",
            "version": metadata.version("mewcode-agent"),
        },
    }
    assert transport.messages[2]["id"] == 2
    assert transport.messages[2]["params"] == {}
    assert transport.marked_version == "2025-11-25"
    assert transport.listener_started is True
    assert snapshot.server_info.name == "fake"
    assert snapshot.server_info.version == "1.0.0"
    assert snapshot.instructions == "untrusted server instructions"
    assert snapshot.list_changed is True
    assert [tool.name for tool in snapshot.tools] == ["echo"]
    await client.close()


async def test_tool_discovery_follows_opaque_cursor_and_sorts_exact_names() -> None:
    transport = FakeTransport()
    transport.pages = {
        None: {"tools": [_tool("z.Tool")], "nextCursor": "opaque-A"},
        "opaque-A": {"tools": [_tool("A-tool")]},
    }
    client = McpClient(_config(), transport)

    snapshot = await client.connect()

    assert [tool.name for tool in snapshot.tools] == ["A-tool", "z.Tool"]
    list_requests = [
        message for message in transport.messages if message.get("method") == "tools/list"
    ]
    assert list_requests[0]["params"] == {}
    assert list_requests[1]["params"] == {"cursor": "opaque-A"}
    await client.close()


@pytest.mark.parametrize(
    ("mutate", "error_type"),
    [
        (
            lambda result: result.update(protocolVersion="2025-06-18"),
            UnsupportedMcpVersion,
        ),
        (
            lambda result: result.update(capabilities={}),
            McpToolsCapabilityMissing,
        ),
        (
            lambda result: result.update(serverInfo={"name": "", "version": "1"}),
            McpProtocolError,
        ),
        (
            lambda result: result.update(instructions={}),
            McpProtocolError,
        ),
    ],
)
async def test_invalid_initialize_response_closes_transport(
    mutate: Callable[[dict[str, Any]], None],
    error_type: type[McpError],
) -> None:
    transport = FakeTransport()
    mutate(transport.initialize_result)
    client = McpClient(_config(), transport)

    with pytest.raises(error_type):
        await client.connect()

    assert transport.closed is True


async def test_duplicate_tool_across_pages_is_rejected() -> None:
    transport = FakeTransport()
    transport.pages = {
        None: {"tools": [_tool("same")], "nextCursor": "next"},
        "next": {"tools": [_tool("same")]},
    }

    with pytest.raises(McpProtocolError, match="重复"):
        await McpClient(_config(), transport).connect()


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "not-a-json-schema-type"},
        {"$schema": "https://unsupported.example/schema", "type": "object"},
        {"properties": {}},
    ],
)
async def test_invalid_or_unsupported_input_schema_is_rejected(
    schema: Mapping[str, Any],
) -> None:
    transport = FakeTransport()
    transport.pages = {None: {"tools": [_tool("bad", input_schema=schema)]}}

    with pytest.raises(McpProtocolError, match="inputSchema"):
        await McpClient(_config(), transport).connect()


async def test_task_required_tool_is_skipped_but_counts_as_discovered() -> None:
    transport = FakeTransport()
    transport.pages = {
        None: {
            "tools": [
                _tool("task-only", task_support="required"),
                _tool("normal", description=None, task_support="optional"),
                _tool("ordinary", task_support="forbidden"),
            ]
        }
    }
    client = McpClient(
        _config(tool_categories={"task-only": "command"}),
        transport,
    )

    snapshot = await client.connect()

    assert [tool.name for tool in snapshot.tools] == ["normal", "ordinary"]
    normal = next(tool for tool in snapshot.tools if tool.name == "normal")
    assert "fake_server" in normal.description
    assert "normal" in normal.description
    assert snapshot.diagnostics[0].code == "mcp_task_required_unsupported"
    await client.close()


async def test_configured_category_for_missing_remote_name_fails_activation() -> None:
    transport = FakeTransport()

    with pytest.raises(McpProtocolError, match="missing-name"):
        await McpClient(
            _config(tool_categories={"missing-name": "read"}),
            transport,
        ).connect()


async def test_cursor_loop_and_page_limit_are_rejected() -> None:
    loop_transport = FakeTransport()
    loop_transport.pages = {
        None: {"tools": [], "nextCursor": "same"},
        "same": {"tools": [], "nextCursor": "same"},
    }
    with pytest.raises(McpProtocolError, match="循环"):
        await McpClient(_config(), loop_transport).connect()

    limit_transport = FakeTransport()

    def page(cursor: str | None) -> dict[str, Any]:
        number = 0 if cursor is None else int(cursor)
        return {"tools": [], "nextCursor": str(number + 1)}

    limit_transport.list_handler = page
    with pytest.raises(McpProtocolError, match="100"):
        await McpClient(_config(), limit_transport).connect()
    list_requests = [
        item
        for item in limit_transport.messages
        if item.get("method") == "tools/list"
    ]
    assert len(list_requests) == 100


async def test_tool_count_limit_is_rejected() -> None:
    transport = FakeTransport()
    transport.pages = {
        None: {"tools": [_tool(f"tool-{index}") for index in range(513)]}
    }

    with pytest.raises(McpProtocolError, match="512"):
        await McpClient(_config(), transport).connect()


async def test_tools_call_preserves_remote_name_and_validates_output_schema() -> None:
    transport = FakeTransport()
    transport.pages = {
        None: {
            "tools": [
                _tool(
                    "Case.Sensitive-tool",
                    output_schema={
                        "type": "object",
                        "properties": {"count": {"type": "integer"}},
                        "required": ["count"],
                    },
                )
            ]
        }
    }
    transport.call_result = {
        "content": [{"type": "text", "text": "done"}],
        "structuredContent": {"count": 2},
        "_meta": {"secret": "internal-only"},
    }
    client = McpClient(_config(), transport)
    await client.connect()

    result = await client.call_tool("Case.Sensitive-tool", {"value": 1})

    call = transport.messages[-1]
    assert call["method"] == "tools/call"
    assert call["params"] == {
        "name": "Case.Sensitive-tool",
        "arguments": {"value": 1},
    }
    assert result.is_error is False
    assert result.structured_content == {"count": 2}
    assert result.meta == {"secret": "internal-only"}
    assert "internal-only" not in repr(result)
    await client.close()


@pytest.mark.parametrize(
    "call_result",
    [
        {"content": "wrong"},
        {"content": [{"type": "unsupported"}]},
        {"content": [], "isError": "false"},
        {"content": []},
        {"content": [], "structuredContent": {"count": "wrong"}},
    ],
)
async def test_invalid_tools_call_result_is_rejected(
    call_result: dict[str, Any],
) -> None:
    transport = FakeTransport()
    transport.pages = {
        None: {
            "tools": [
                _tool(
                    "typed",
                    output_schema={
                        "type": "object",
                        "properties": {"count": {"type": "integer"}},
                        "required": ["count"],
                    },
                )
            ]
        }
    }
    transport.call_result = call_result
    client = McpClient(_config(), transport)
    await client.connect()

    with pytest.raises(McpInvalidToolResult):
        await client.call_tool("typed", {})

    await client.close()


async def test_output_schema_remote_reference_is_not_retrieved() -> None:
    transport = FakeTransport()
    transport.pages = {
        None: {
            "tools": [
                _tool(
                    "remote-ref",
                    output_schema={
                        "type": "object",
                        "properties": {
                            "value": {"$ref": "https://schemas.example.test/value.json"}
                        },
                    },
                )
            ]
        }
    }
    transport.call_result = {
        "content": [],
        "structuredContent": {"value": 1},
    }
    client = McpClient(_config(), transport)
    await client.connect()

    with pytest.raises(McpInvalidToolResult):
        await client.call_tool("remote-ref", {})

    await client.close()


async def test_error_tool_result_does_not_require_structured_content() -> None:
    transport = FakeTransport()
    transport.pages = {
        None: {
            "tools": [
                _tool("typed", output_schema={"type": "object"})
            ]
        }
    }
    transport.call_result = {
        "content": [{"type": "text", "text": "bad arguments"}],
        "isError": True,
    }
    client = McpClient(_config(), transport)
    await client.connect()

    result = await client.call_tool("typed", {})

    assert result.is_error is True
    await client.close()


async def test_unknown_remote_tool_is_not_sent() -> None:
    transport = FakeTransport()
    client = McpClient(_config(), transport)
    await client.connect()
    before = len(transport.messages)

    with pytest.raises(McpToolNotFound):
        await client.call_tool("missing", {})

    assert len(transport.messages) == before
    await client.close()


async def test_list_changed_notifications_are_coalesced_and_refresh_atomically() -> None:
    transport = FakeTransport()
    transport.initialize_result["capabilities"]["tools"]["listChanged"] = True
    client = McpClient(_config(), transport)
    await client.connect()
    transport.pages = {None: {"tools": [_tool("new-tool")]}}
    started = asyncio.Event()
    release = asyncio.Event()
    refresh_count = 0

    async def refresh() -> None:
        nonlocal refresh_count
        refresh_count += 1
        started.set()
        await release.wait()
        await client.discover_tools()

    client.set_tools_changed_handler(refresh)
    await transport.emit_notification("notifications/tools/list_changed")
    await asyncio.wait_for(started.wait(), timeout=1)
    await transport.emit_notification("notifications/tools/list_changed")
    release.set()

    async def wait_for_refresh() -> None:
        while [tool.name for tool in client.tools] != ["new-tool"]:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_refresh(), timeout=1)
    assert refresh_count == 1
    assert [tool.name for tool in client.tools] == ["new-tool"]
    await client.close()


async def test_reinitialize_creates_new_json_rpc_id_sequence() -> None:
    transport = FakeTransport()
    client = McpClient(_config(), transport)
    await client.connect()

    await client.reinitialize()

    initialize_ids = [
        message["id"]
        for message in transport.messages
        if message.get("method") == "initialize"
    ]
    assert initialize_ids == [1, 1]
    assert transport.reset_count == 1
    await client.close()


async def test_initial_tools_list_session_404_reinitializes_once() -> None:
    transport = FakeTransport()
    transport.expire_list_calls = 1
    client = McpClient(_config(), transport)

    snapshot = await client.connect()

    initialize_ids = [
        message["id"]
        for message in transport.messages
        if message.get("method") == "initialize"
    ]
    assert [tool.name for tool in snapshot.tools] == ["echo"]
    assert initialize_ids == [1, 1]
    assert transport.reset_count == 1
    await client.close()
