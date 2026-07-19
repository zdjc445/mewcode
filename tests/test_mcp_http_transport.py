from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from typing import Any

import httpx
import pytest

from mewcode_agent.mcp import (
    MAX_MCP_MESSAGE_BYTES,
    JsonRpcNotification,
    McpConnectFailed,
    McpConnectionLost,
    McpError,
    McpMessageTooLarge,
    McpProtocolError,
    McpSessionExpired,
    McpShutdownFailed,
    StreamableHttpServerConfig,
    decode_json_rpc_message,
)
from mewcode_agent.mcp.transports import (
    StreamableHttpTransport,
    iter_sse_events,
)


def _config(*, headers: dict[str, str] | None = None) -> StreamableHttpServerConfig:
    return StreamableHttpServerConfig(
        server_id="fake_http",
        required=True,
        url="https://mcp.example.test/endpoint",
        headers=headers or {},
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        shutdown_timeout_seconds=1,
        tool_categories={},
    )


async def _chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


async def _wait_until(predicate: Any, *, timeout: float = 1) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout=timeout)


async def test_sse_parser_supports_multiline_data_id_retry_and_empty_data() -> None:
    events = [
        event
        async for event in iter_sse_events(
            _chunks(
                b": comment\nid: cursor-A\nretry: 25\ndata: first\r\n",
                b"data: second\n\nid: cursor-B\ndata:\n\n",
            )
        )
    ]

    assert events[0].data == "first\nsecond"
    assert events[0].event_id == "cursor-A"
    assert events[0].retry_milliseconds == 25
    assert events[1].data == ""
    assert events[1].event_id == "cursor-B"


async def test_sse_parser_rejects_invalid_utf8_and_oversized_line() -> None:
    with pytest.raises(McpProtocolError):
        _ = [event async for event in iter_sse_events(_chunks(b"data: \xff\n\n"))]
    with pytest.raises(McpMessageTooLarge):
        _ = [
            event
            async for event in iter_sse_events(
                _chunks(b"x" * (MAX_MCP_MESSAGE_BYTES + 1))
            )
        ]


async def test_http_json_session_headers_notification_and_delete() -> None:
    requests: list[httpx.Request] = []
    session_id = "opaque-session-value"

    async def server(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        message = json.loads(request.content)
        if message.get("method") == "initialize":
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "MCP-Session-Id": session_id,
                },
                json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
            )
        return httpx.Response(202)

    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    transport = StreamableHttpTransport(
        _config(headers={"Authorization": "Bearer secret"}),
        client=client,
    )
    received: list[Any] = []

    async def on_message(payload: bytes | str) -> None:
        received.append(decode_json_rpc_message(payload))

    await transport.connect(on_message, lambda error: None)
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    transport.mark_initialized("2025-11-25")
    await transport.send(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    await transport.close()

    assert received[0].result == {"ok": True}
    assert transport.has_session is True
    initialize, notification, delete = requests
    assert initialize.headers["accept"] == "application/json, text/event-stream"
    assert initialize.headers["content-type"] == "application/json"
    assert "MCP-Protocol-Version" not in initialize.headers
    assert "MCP-Session-Id" not in initialize.headers
    assert notification.headers["MCP-Protocol-Version"] == "2025-11-25"
    assert notification.headers["MCP-Session-Id"] == session_id
    assert delete.headers["MCP-Protocol-Version"] == "2025-11-25"
    assert delete.headers["MCP-Session-Id"] == session_id


async def test_http_post_sse_delivers_response() -> None:
    async def server(request: httpx.Request) -> httpx.Response:
        body = (
            b"id: post-event\n"
            b'data: {"jsonrpc":"2.0","id":7,"result":{"value":1}}\n\n'
        )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=body,
        )

    transport = StreamableHttpTransport(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    received: list[Any] = []

    async def on_message(payload: bytes | str) -> None:
        received.append(decode_json_rpc_message(payload))

    await transport.connect(on_message, lambda error: None)
    try:
        await transport.send(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}
        )
        assert received[0].result == {"value": 1}
    finally:
        await transport.close()


async def test_http_post_sse_recovers_without_reposting_request() -> None:
    requests: list[httpx.Request] = []

    async def server(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=b"retry: 1\nid: post-cursor\ndata:\n\n",
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=(
                b'id: final\ndata: {"jsonrpc":"2.0","id":7,'
                b'"result":{"recovered":true}}\n\n'
            ),
        )

    transport = StreamableHttpTransport(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    received: list[Any] = []

    async def on_message(payload: bytes | str) -> None:
        received.append(decode_json_rpc_message(payload))

    await transport.connect(on_message, lambda error: None)
    try:
        await transport.send(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}}
        )
        assert [request.method for request in requests] == ["POST", "GET"]
        assert requests[1].headers["Last-Event-ID"] == "post-cursor"
        assert received[0].result == {"recovered": True}
    finally:
        await transport.close()


async def test_http_get_405_keeps_transport_available() -> None:
    methods: list[str] = []

    async def server(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )
        if request.method == "GET":
            return httpx.Response(405)
        return httpx.Response(200)

    transport = StreamableHttpTransport(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    await transport.connect(lambda payload: asyncio.sleep(0), lambda error: None)
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    transport.mark_initialized("2025-11-25")
    await transport.start_listener()
    await transport.close()

    assert transport.listener_supported is False
    assert methods == ["POST", "GET"]


async def test_http_get_sse_reconnects_with_last_event_id() -> None:
    get_requests: list[httpx.Request] = []
    notification_received = asyncio.Event()

    async def server(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "MCP-Session-Id": "session-for-get",
                },
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )
        if request.method == "DELETE":
            return httpx.Response(200)
        get_requests.append(request)
        if len(get_requests) == 1:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=(
                    b"retry: 1\nid: cursor-A\n"
                    b'data: {"jsonrpc":"2.0","method":"notifications/test"}\n\n'
                ),
            )
        return httpx.Response(405)

    transport = StreamableHttpTransport(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )

    async def on_message(payload: bytes | str) -> None:
        message = decode_json_rpc_message(payload)
        if (
            isinstance(message, JsonRpcNotification)
            and message.method == "notifications/test"
        ):
            notification_received.set()

    await transport.connect(on_message, lambda error: None)
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    transport.mark_initialized("2025-11-25")
    await transport.start_listener()
    await asyncio.wait_for(notification_received.wait(), timeout=1)
    await _wait_until(lambda: len(get_requests) == 2)
    await transport.close()

    assert "Last-Event-ID" not in get_requests[0].headers
    assert get_requests[1].headers["Last-Event-ID"] == "cursor-A"
    assert get_requests[1].headers["MCP-Session-Id"] == "session-for-get"


async def test_http_session_404_is_distinct_and_not_retried() -> None:
    post_count = 0

    async def server(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "DELETE":
            return httpx.Response(200)
        post_count += 1
        if post_count == 1:
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "MCP-Session-Id": "expired-session",
                },
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )
        return httpx.Response(404)

    transport = StreamableHttpTransport(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    await transport.connect(lambda payload: asyncio.sleep(0), lambda error: None)
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    transport.mark_initialized("2025-11-25")
    try:
        with pytest.raises(McpSessionExpired):
            await transport.send(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            )
        assert post_count == 2
    finally:
        await transport.close()


@pytest.mark.parametrize(
    ("status", "content_type", "message", "error_type"),
    [
        (307, "application/json", {"id": 1, "method": "tools/list"}, McpProtocolError),
        (500, "application/json", {"id": 1, "method": "initialize"}, McpConnectFailed),
        (500, "application/json", {"id": 1, "method": "tools/list"}, McpConnectionLost),
        (200, "text/html", {"id": 1, "method": "tools/list"}, McpProtocolError),
        (202, "application/json", {"id": 1, "method": "tools/list"}, McpProtocolError),
    ],
)
async def test_http_rejects_invalid_status_or_content_type(
    status: int,
    content_type: str,
    message: dict[str, Any],
    error_type: type[McpError],
) -> None:
    async def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"Content-Type": content_type, "Location": "/redirect"},
        )

    transport = StreamableHttpTransport(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    await transport.connect(lambda payload: asyncio.sleep(0), lambda error: None)
    outbound = {"jsonrpc": "2.0", "params": {}, **message}
    try:
        with pytest.raises(error_type):
            await transport.send(outbound)
    finally:
        await transport.close()


async def test_http_rejects_body_on_accepted_notification() -> None:
    async def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, content=b"unexpected")

    transport = StreamableHttpTransport(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    await transport.connect(lambda payload: asyncio.sleep(0), lambda error: None)
    try:
        with pytest.raises(McpProtocolError, match="202"):
            await transport.send(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
    finally:
        await transport.close()


async def test_http_response_limit_is_enforced() -> None:
    async def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b"x" * (MAX_MCP_MESSAGE_BYTES + 1),
        )

    transport = StreamableHttpTransport(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    await transport.connect(lambda payload: asyncio.sleep(0), lambda error: None)
    try:
        with pytest.raises(McpMessageTooLarge):
            await transport.send(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            )
    finally:
        await transport.close()


async def test_http_secret_does_not_enter_repr_or_status_error() -> None:
    secret = "Bearer private-token"

    async def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = StreamableHttpTransport(
        _config(headers={"Authorization": secret}),
        client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    await transport.connect(lambda payload: asyncio.sleep(0), lambda error: None)
    try:
        with pytest.raises(McpConnectionLost) as caught:
            await transport.send(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            )
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)
        assert secret not in repr(transport)
    finally:
        await transport.close()


async def test_http_delete_failure_still_closes_client() -> None:
    async def server(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "MCP-Session-Id": "session-to-close",
                },
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    transport = StreamableHttpTransport(_config(), client=client)
    await transport.connect(lambda payload: asyncio.sleep(0), lambda error: None)
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    transport.mark_initialized("2025-11-25")

    with pytest.raises(McpShutdownFailed):
        await transport.close()

    assert client.is_closed is True
