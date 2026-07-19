"""Strict JSON-RPC 2.0 parsing and asynchronous request routing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import json
from typing import Any, TypeAlias, cast

from mewcode_agent.mcp.models import (
    MAX_MCP_ERROR_MESSAGE_BYTES,
    MAX_MCP_MESSAGE_BYTES,
    McpConnectionLost,
    McpError,
    McpMessageTooLarge,
    McpProtocolError,
    McpRequestTimeout,
)

JsonRpcId: TypeAlias = int | str
SendMessage: TypeAlias = Callable[[dict[str, Any]], Awaitable[None]]
NotificationHandler: TypeAlias = Callable[[Any | None], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    request_id: JsonRpcId
    method: str
    params: Any | None = None


@dataclass(frozen=True, slots=True)
class JsonRpcNotification:
    method: str
    params: Any | None = None


@dataclass(frozen=True, slots=True)
class JsonRpcSuccessResponse:
    request_id: JsonRpcId
    result: Any


@dataclass(frozen=True, slots=True)
class JsonRpcErrorObject:
    code: int
    message: str
    data: Any | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class JsonRpcErrorResponse:
    request_id: JsonRpcId
    error: JsonRpcErrorObject


JsonRpcMessage: TypeAlias = (
    JsonRpcRequest
    | JsonRpcNotification
    | JsonRpcSuccessResponse
    | JsonRpcErrorResponse
)


class JsonRpcRemoteError(McpProtocolError):
    """A safe representation of a server JSON-RPC error response."""

    def __init__(self, rpc_code: int, message: str, *, data: Any = None) -> None:
        safe_message = _truncate_utf8(message, MAX_MCP_ERROR_MESSAGE_BYTES)
        super().__init__(f"JSON-RPC error {rpc_code}: {safe_message}")
        self.rpc_code = rpc_code
        self.remote_message = safe_message
        self.data = data


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value
    marker = "…"
    budget = limit - len(marker.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + marker


def _valid_id(value: Any) -> bool:
    if type(value) is int:
        return True
    return isinstance(value, str) and bool(value)


def _validate_params(value: Any) -> None:
    if not isinstance(value, (Mapping, list)):
        raise McpProtocolError("JSON-RPC params 必须是 object 或 array")


def parse_json_rpc_message(value: Any) -> JsonRpcMessage:
    """Validate and classify one decoded JSON-RPC object."""

    if not isinstance(value, Mapping):
        raise McpProtocolError("JSON-RPC 消息必须是单个 object")
    if any(not isinstance(key, str) for key in value):
        raise McpProtocolError("JSON-RPC 消息字段名必须是字符串")
    message = cast(Mapping[str, Any], value)
    if message.get("jsonrpc") != "2.0":
        raise McpProtocolError('JSON-RPC 消息必须包含 jsonrpc: "2.0"')

    if "method" in message:
        if "result" in message or "error" in message:
            raise McpProtocolError("JSON-RPC request 不能包含 result 或 error")
        method = message["method"]
        if not isinstance(method, str) or not method:
            raise McpProtocolError("JSON-RPC method 必须是非空字符串")
        params = message.get("params")
        if "params" in message:
            _validate_params(params)
        if "id" not in message:
            return JsonRpcNotification(method=method, params=params)
        request_id = message["id"]
        if not _valid_id(request_id):
            raise McpProtocolError("JSON-RPC request id 类型无效")
        return JsonRpcRequest(
            request_id=cast(JsonRpcId, request_id),
            method=method,
            params=params,
        )

    if "id" not in message or not _valid_id(message.get("id")):
        raise McpProtocolError("JSON-RPC response id 类型无效")
    if "params" in message:
        raise McpProtocolError("JSON-RPC response 不能包含 params")
    has_result = "result" in message
    has_error = "error" in message
    if has_result == has_error:
        raise McpProtocolError("JSON-RPC response 必须且只能包含 result 或 error")
    request_id = cast(JsonRpcId, message["id"])
    if has_result:
        return JsonRpcSuccessResponse(
            request_id=request_id,
            result=message["result"],
        )

    raw_error = message["error"]
    if not isinstance(raw_error, Mapping):
        raise McpProtocolError("JSON-RPC error 必须是 object")
    allowed_error_keys = {"code", "message", "data"}
    if any(not isinstance(key, str) for key in raw_error):
        raise McpProtocolError("JSON-RPC error 字段名必须是字符串")
    unknown = set(raw_error) - allowed_error_keys
    if unknown:
        raise McpProtocolError("JSON-RPC error 包含未知字段")
    code = raw_error.get("code")
    error_message = raw_error.get("message")
    if type(code) is not int or not isinstance(error_message, str):
        raise McpProtocolError("JSON-RPC error 的 code 或 message 类型无效")
    return JsonRpcErrorResponse(
        request_id=request_id,
        error=JsonRpcErrorObject(
            code=code,
            message=error_message,
            data=raw_error.get("data"),
        ),
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonstandard_number(value: str) -> None:
    raise ValueError(f"invalid JSON number: {value}")


def decode_json_rpc_message(payload: bytes | str) -> JsonRpcMessage:
    """Decode one bounded UTF-8 JSON-RPC message."""

    try:
        encoded = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    except UnicodeError as exc:
        raise McpProtocolError("JSON-RPC 消息不是有效 UTF-8") from exc
    if len(encoded) > MAX_MCP_MESSAGE_BYTES:
        raise McpMessageTooLarge()
    try:
        text = encoded.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise McpProtocolError("JSON-RPC 消息不是有效 JSON") from exc
    return parse_json_rpc_message(value)


def encode_json_rpc_message(message: Mapping[str, Any]) -> bytes:
    """Encode one bounded JSON-RPC object without literal newlines."""

    try:
        payload = json.dumps(
            dict(message),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise McpProtocolError("无法编码 JSON-RPC 消息") from exc
    if len(payload) > MAX_MCP_MESSAGE_BYTES:
        raise McpMessageTooLarge()
    return payload


class JsonRpcSession:
    """Match concurrent requests with responses for one transport session."""

    def __init__(self, send_message: SendMessage) -> None:
        self._send_message = send_message
        self._next_request_id = 1
        self._pending: dict[JsonRpcId, asyncio.Future[Any]] = {}
        self._completed_response_ids: set[JsonRpcId] = set()
        self._ignored_response_ids: set[JsonRpcId] = set()
        self._notification_handlers: dict[str, NotificationHandler] = {}
        self._closed_error: McpError | None = None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def closed(self) -> bool:
        return self._closed_error is not None

    def set_notification_handler(
        self,
        method: str,
        handler: NotificationHandler,
    ) -> None:
        if not isinstance(method, str) or not method:
            raise ValueError("notification method 必须是非空字符串")
        self._notification_handlers[method] = handler

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | list[Any] | None = None,
        *,
        timeout_seconds: float,
    ) -> Any:
        if self._closed_error is not None:
            raise self._closed_error
        if not isinstance(method, str) or not method:
            raise ValueError("request method 必须是非空字符串")
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是大于 0 的数字")
        request_id = self._next_request_id
        self._next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            _validate_params(params)
            message["params"] = params
        try:
            await self._send_message(message)
        except BaseException:
            self._abandon(request_id, future)
            raise

        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=float(timeout_seconds),
            )
        except TimeoutError as exc:
            self._abandon(request_id, future)
            if method != "initialize":
                await self._send_cancellation(request_id)
            raise McpRequestTimeout(f"MCP 请求超时: {method}") from exc
        except asyncio.CancelledError:
            self._abandon(request_id, future)
            if method != "initialize":
                await self._send_cancellation(request_id)
            raise

    async def notify(
        self,
        method: str,
        params: Mapping[str, Any] | list[Any] | None = None,
    ) -> None:
        if self._closed_error is not None:
            raise self._closed_error
        if not isinstance(method, str) or not method:
            raise ValueError("notification method 必须是非空字符串")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            _validate_params(params)
            message["params"] = params
        await self._send_message(message)

    async def receive(self, value: Any) -> None:
        message = (
            decode_json_rpc_message(value)
            if isinstance(value, (bytes, str))
            else parse_json_rpc_message(value)
        )
        if isinstance(message, (JsonRpcSuccessResponse, JsonRpcErrorResponse)):
            self._receive_response(message)
            return
        if isinstance(message, JsonRpcRequest):
            await self._receive_request(message)
            return
        handler = self._notification_handlers.get(message.method)
        if handler is not None:
            await handler(message.params)

    def close(self, error: McpError | None = None) -> None:
        if self._closed_error is not None:
            return
        self._closed_error = error or McpConnectionLost()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self._closed_error)
        self._pending.clear()

    def _receive_response(
        self,
        message: JsonRpcSuccessResponse | JsonRpcErrorResponse,
    ) -> None:
        request_id = message.request_id
        if request_id in self._ignored_response_ids:
            self._ignored_response_ids.remove(request_id)
            return
        if request_id in self._completed_response_ids:
            raise McpProtocolError("收到重复的 JSON-RPC response id")
        future = self._pending.pop(request_id, None)
        if future is None:
            raise McpProtocolError("收到未知的 JSON-RPC response id")
        self._completed_response_ids.add(request_id)
        if isinstance(message, JsonRpcErrorResponse):
            future.set_exception(
                JsonRpcRemoteError(
                    message.error.code,
                    message.error.message,
                    data=message.error.data,
                )
            )
        else:
            future.set_result(message.result)

    async def _receive_request(self, message: JsonRpcRequest) -> None:
        if message.method == "ping":
            response: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": message.request_id,
                "result": {},
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": message.request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        await self._send_message(response)

    def _abandon(
        self,
        request_id: JsonRpcId,
        future: asyncio.Future[Any],
    ) -> None:
        if self._pending.pop(request_id, None) is not None:
            self._ignored_response_ids.add(request_id)
        if not future.done():
            future.cancel()

    async def _send_cancellation(self, request_id: JsonRpcId) -> None:
        if self._closed_error is not None:
            return
        try:
            await self._send_message(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": request_id},
                }
            )
        except Exception:
            return
