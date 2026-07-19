from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from mewcode_agent.mcp import (
    MAX_MCP_ERROR_MESSAGE_BYTES,
    MAX_MCP_MESSAGE_BYTES,
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcRemoteError,
    JsonRpcRequest,
    JsonRpcSession,
    JsonRpcSuccessResponse,
    McpConnectionLost,
    McpMessageTooLarge,
    McpProtocolError,
    McpRequestTimeout,
    decode_json_rpc_message,
    encode_json_rpc_message,
    parse_json_rpc_message,
)


def test_parse_all_supported_message_shapes() -> None:
    request = parse_json_rpc_message(
        {"jsonrpc": "2.0", "id": "server-1", "method": "ping", "params": {}}
    )
    notification = parse_json_rpc_message(
        {"jsonrpc": "2.0", "method": "notifications/test"}
    )
    success = parse_json_rpc_message(
        {"jsonrpc": "2.0", "id": 1, "result": None}
    )
    error = parse_json_rpc_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": -32000, "message": "failed", "data": {"x": 1}},
        }
    )

    assert request == JsonRpcRequest("server-1", "ping", {})
    assert notification == JsonRpcNotification("notifications/test")
    assert success == JsonRpcSuccessResponse(1, None)
    assert isinstance(error, JsonRpcErrorResponse)
    assert error.error.code == -32000
    assert error.error.data == {"x": 1}


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"id": 1, "result": {}},
        {"jsonrpc": "1.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "method": ""},
        {"jsonrpc": "2.0", "method": "ping", "id": None},
        {"jsonrpc": "2.0", "method": "ping", "id": True},
        {"jsonrpc": "2.0", "method": "ping", "id": 1.5},
        {"jsonrpc": "2.0", "method": "ping", "params": "wrong"},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "params": {}},
        {"jsonrpc": "2.0", "id": 1, "error": []},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": True, "message": "x"}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": 1, "message": 2}},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": 1, "message": "x", "unknown": True},
        },
    ],
)
def test_invalid_decoded_message_is_rejected(value: Any) -> None:
    with pytest.raises(McpProtocolError):
        parse_json_rpc_message(value)


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"jsonrpc":"2.0","id":1,"id":2,"result":{}}',
        b'{"jsonrpc":"2.0","id":1,"result":NaN}',
        b"\xff",
    ],
)
def test_invalid_encoded_message_is_rejected(payload: bytes) -> None:
    with pytest.raises(McpProtocolError):
        decode_json_rpc_message(payload)


def test_message_size_limit_is_enforced() -> None:
    payload = b" " * (MAX_MCP_MESSAGE_BYTES + 1)

    with pytest.raises(McpMessageTooLarge):
        decode_json_rpc_message(payload)
    with pytest.raises(McpMessageTooLarge):
        encode_json_rpc_message(
            {
                "jsonrpc": "2.0",
                "method": "test",
                "params": {"value": "x" * MAX_MCP_MESSAGE_BYTES},
            }
        )


def test_encoder_uses_utf8_and_escapes_string_newlines() -> None:
    payload = encode_json_rpc_message(
        {
            "jsonrpc": "2.0",
            "method": "test",
            "params": {"value": "第一行\n第二行"},
        }
    )

    assert "第一行".encode() in payload
    assert b"\\n" in payload
    assert b"\n" not in payload


class MessageSink:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.sent = asyncio.Event()

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self.sent.set()

    async def wait_for_count(self, count: int) -> None:
        while len(self.messages) < count:
            self.sent.clear()
            await self.sent.wait()


async def test_concurrent_requests_use_monotonic_ids_and_match_out_of_order() -> None:
    sink = MessageSink()
    session = JsonRpcSession(sink)

    first = asyncio.create_task(
        session.request("first", {"value": 1}, timeout_seconds=1)
    )
    second = asyncio.create_task(
        session.request("second", {"value": 2}, timeout_seconds=1)
    )
    await sink.wait_for_count(2)
    assert [message["id"] for message in sink.messages] == [1, 2]

    await session.receive({"jsonrpc": "2.0", "id": 2, "result": "second"})
    await session.receive({"jsonrpc": "2.0", "id": 1, "result": "first"})

    assert await first == "first"
    assert await second == "second"
    assert session.pending_count == 0


async def test_remote_error_is_delivered_to_matching_request_and_truncated() -> None:
    sink = MessageSink()
    session = JsonRpcSession(sink)
    request = asyncio.create_task(
        session.request("tools/call", {}, timeout_seconds=1)
    )
    await sink.wait_for_count(1)
    remote_message = "错" * MAX_MCP_ERROR_MESSAGE_BYTES

    await session.receive(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32001,
                "message": remote_message,
                "data": {"secret": "not-in-repr"},
            },
        }
    )

    with pytest.raises(JsonRpcRemoteError) as caught:
        await request
    assert caught.value.code == "mcp_protocol_error"
    assert caught.value.rpc_code == -32001
    assert len(caught.value.remote_message.encode("utf-8")) <= (
        MAX_MCP_ERROR_MESSAGE_BYTES
    )
    assert caught.value.data == {"secret": "not-in-repr"}
    assert "not-in-repr" not in repr(caught.value)


async def test_ping_and_unknown_server_request_receive_responses() -> None:
    sink = MessageSink()
    session = JsonRpcSession(sink)

    await session.receive({"jsonrpc": "2.0", "id": "p1", "method": "ping"})
    await session.receive(
        {"jsonrpc": "2.0", "id": "u1", "method": "unsupported/request"}
    )

    assert sink.messages == [
        {"jsonrpc": "2.0", "id": "p1", "result": {}},
        {
            "jsonrpc": "2.0",
            "id": "u1",
            "error": {"code": -32601, "message": "Method not found"},
        },
    ]


async def test_known_notification_runs_handler_and_unknown_is_ignored() -> None:
    sink = MessageSink()
    session = JsonRpcSession(sink)
    received: list[Any] = []

    async def handler(params: Any | None) -> None:
        received.append(params)

    session.set_notification_handler("notifications/tools/list_changed", handler)
    await session.receive(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tools/list_changed",
            "params": {"reason": "changed"},
        }
    )
    await session.receive({"jsonrpc": "2.0", "method": "unknown"})

    assert received == [{"reason": "changed"}]
    assert sink.messages == []


async def test_timeout_sends_cancellation_and_late_response_is_ignored() -> None:
    sink = MessageSink()
    session = JsonRpcSession(sink)

    with pytest.raises(McpRequestTimeout):
        await session.request("tools/list", {}, timeout_seconds=0.001)

    assert sink.messages == [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 1},
        },
    ]
    await session.receive({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert session.pending_count == 0


async def test_initialize_timeout_does_not_send_cancellation() -> None:
    sink = MessageSink()
    session = JsonRpcSession(sink)

    with pytest.raises(McpRequestTimeout):
        await session.request("initialize", {}, timeout_seconds=0.001)

    assert len(sink.messages) == 1


async def test_outer_cancellation_sends_protocol_cancellation() -> None:
    sink = MessageSink()
    session = JsonRpcSession(sink)
    task = asyncio.create_task(
        session.request("tools/call", {}, timeout_seconds=10)
    )
    await sink.wait_for_count(1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sink.messages[-1] == {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 1},
    }
    await session.receive({"jsonrpc": "2.0", "id": 1, "result": {}})


async def test_close_fails_all_pending_requests_with_same_error() -> None:
    sink = MessageSink()
    session = JsonRpcSession(sink)
    first = asyncio.create_task(session.request("one", timeout_seconds=10))
    second = asyncio.create_task(session.request("two", timeout_seconds=10))
    await sink.wait_for_count(2)
    error = McpConnectionLost("test close")

    session.close(error)
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert results[0] is error
    assert results[1] is error
    assert session.pending_count == 0
    assert session.closed is True
    with pytest.raises(McpConnectionLost) as caught:
        await session.request("after-close", timeout_seconds=1)
    assert caught.value is error


async def test_unknown_and_duplicate_response_ids_are_protocol_errors() -> None:
    sink = MessageSink()
    session = JsonRpcSession(sink)

    with pytest.raises(McpProtocolError, match="未知"):
        await session.receive({"jsonrpc": "2.0", "id": 99, "result": {}})

    request = asyncio.create_task(session.request("one", timeout_seconds=1))
    await sink.wait_for_count(1)
    response: Mapping[str, Any] = {"jsonrpc": "2.0", "id": 1, "result": {}}
    await session.receive(response)
    await request
    with pytest.raises(McpProtocolError, match="重复"):
        await session.receive(response)
