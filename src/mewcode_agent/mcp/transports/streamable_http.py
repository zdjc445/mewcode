"""MCP Streamable HTTP transport with JSON, SSE, and session handling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx

from mewcode_agent.mcp.models import (
    MAX_MCP_MESSAGE_BYTES,
    MCP_PROTOCOL_VERSION,
    McpConnectFailed,
    McpConnectionLost,
    McpError,
    McpMessageTooLarge,
    McpProtocolError,
    McpSessionExpired,
    McpShutdownFailed,
    StreamableHttpServerConfig,
)
from mewcode_agent.mcp.protocol import (
    JsonRpcErrorResponse,
    JsonRpcSuccessResponse,
    decode_json_rpc_message,
    encode_json_rpc_message,
)
from mewcode_agent.mcp.transports.base import (
    CloseHandler,
    InboundMessageHandler,
    McpTransport,
)


@dataclass(frozen=True, slots=True)
class SseEvent:
    data: str | None
    event_id: str | None
    retry_milliseconds: int | None


async def iter_sse_events(
    chunks: AsyncIterable[bytes],
) -> AsyncIterator[SseEvent]:
    """Parse a UTF-8 SSE byte stream while enforcing the MCP message limit."""

    buffer = bytearray()
    data_lines: list[str] = []
    data_size = 0
    event_id: str | None = None
    retry_milliseconds: int | None = None
    saw_field = False

    async def dispatch() -> SseEvent | None:
        nonlocal data_lines, data_size, event_id, retry_milliseconds, saw_field
        if not saw_field:
            return None
        event = SseEvent(
            data="\n".join(data_lines) if data_lines else None,
            event_id=event_id,
            retry_milliseconds=retry_milliseconds,
        )
        data_lines = []
        data_size = 0
        event_id = None
        retry_milliseconds = None
        saw_field = False
        return event

    async for chunk in chunks:
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                if len(buffer) > MAX_MCP_MESSAGE_BYTES:
                    raise McpMessageTooLarge()
                break
            line_bytes = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]
            if len(line_bytes) > MAX_MCP_MESSAGE_BYTES:
                raise McpMessageTooLarge()
            try:
                line = line_bytes.decode("utf-8")
            except UnicodeError as exc:
                raise McpProtocolError("MCP SSE 不是有效 UTF-8") from exc
            if not line:
                event = await dispatch()
                if event is not None:
                    yield event
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "data":
                encoded_size = len(value.encode("utf-8"))
                data_size += encoded_size + (1 if data_lines else 0)
                if data_size > MAX_MCP_MESSAGE_BYTES:
                    raise McpMessageTooLarge()
                data_lines.append(value)
                saw_field = True
            elif field == "id" and "\0" not in value:
                event_id = value
                saw_field = True
            elif field == "retry" and value.isdecimal():
                retry_milliseconds = int(value)
                saw_field = True

    if buffer:
        if len(buffer) > MAX_MCP_MESSAGE_BYTES:
            raise McpMessageTooLarge()
        try:
            line = buffer.rstrip(b"\r").decode("utf-8")
        except UnicodeError as exc:
            raise McpProtocolError("MCP SSE 不是有效 UTF-8") from exc
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
            saw_field = True
    event = await dispatch()
    if event is not None:
        yield event


class StreamableHttpTransport(McpTransport):
    """One reusable MCP Streamable HTTP session."""

    def __init__(
        self,
        config: StreamableHttpServerConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._on_message: InboundMessageHandler | None = None
        self._on_close: CloseHandler | None = None
        self._session_id: str | None = None
        self._initialized = False
        self._connected = False
        self._closing = False
        self._closed = False
        self._listener_task: asyncio.Task[None] | None = None
        self._listener_supported: bool | None = None
        self._last_get_event_id: str | None = None
        self._get_retry_seconds: float | None = None
        self._failure: McpError | None = None
        self._close_lock = asyncio.Lock()
        self._long_stream_timeout = httpx.Timeout(
            None,
            connect=self._config.connect_timeout_seconds,
        )

    @property
    def listener_supported(self) -> bool | None:
        return self._listener_supported

    @property
    def has_session(self) -> bool:
        return self._session_id is not None

    @property
    def failure(self) -> McpError | None:
        return self._failure

    async def connect(
        self,
        on_message: InboundMessageHandler,
        on_close: CloseHandler,
    ) -> None:
        if self._connected or self._closed:
            raise McpConnectFailed(
                f"MCP HTTP server {self._config.server_id} 的 transport 状态无效"
            )
        self._on_message = on_message
        self._on_close = on_close
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=dict(self._config.headers),
                follow_redirects=False,
                timeout=httpx.Timeout(
                    self._config.request_timeout_seconds,
                    connect=self._config.connect_timeout_seconds,
                ),
            )
        self._connected = True

    async def send(self, message: Mapping[str, Any]) -> None:
        if self._failure is not None:
            raise self._failure
        if not self._connected or self._closed or self._closing:
            raise self._failure or McpConnectionLost(
                f"MCP HTTP server {self._config.server_id} 未连接"
            )
        client = self._require_client()
        payload = encode_json_rpc_message(message)
        headers = self._headers(
            accept="application/json, text/event-stream",
            content_type="application/json",
        )
        is_initialize = message.get("method") == "initialize"
        request_id = (
            message.get("id")
            if "method" in message and "id" in message
            else None
        )
        try:
            timeout = (
                self._long_stream_timeout
                if message.get("method") == "tools/call"
                else self._config.request_timeout_seconds
            )
            async with client.stream(
                "POST",
                self._config.url,
                headers=headers,
                content=payload,
                timeout=timeout,
            ) as response:
                self._validate_common_response(response, message)
                if is_initialize:
                    self._capture_session(response)
                if response.status_code == 202:
                    body = await self._read_limited(response)
                    if body:
                        raise McpProtocolError(
                            "MCP HTTP 202 response 必须没有正文"
                        )
                    if request_id is not None:
                        raise McpProtocolError(
                            "MCP HTTP request 不能以 202 代替 JSON-RPC response"
                        )
                    return
                content_type = self._content_type(response)
                if content_type == "application/json":
                    body = await self._read_limited(response)
                    if not body:
                        raise McpProtocolError("MCP HTTP JSON response 正文为空")
                    await self._deliver(body)
                    return
                if content_type == "text/event-stream":
                    await self._consume_post_sse(response, request_id)
                    return
                raise McpProtocolError("MCP HTTP response Content-Type 无效")
        except McpError:
            raise
        except httpx.TimeoutException as exc:
            raise self._request_failure(message, "请求超时") from exc
        except httpx.HTTPError as exc:
            raise self._request_failure(message, "网络失败") from exc

    def mark_initialized(self, protocol_version: str) -> None:
        if protocol_version != MCP_PROTOCOL_VERSION:
            raise ValueError("HTTP transport 收到不支持的 MCP 协议版本")
        self._initialized = True

    async def reset_session(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            await asyncio.gather(self._listener_task, return_exceptions=True)
            self._listener_task = None
            self._listener_supported = None
        self._session_id = None
        self._initialized = False
        self._last_get_event_id = None
        self._get_retry_seconds = None
        self._failure = None

    async def start_listener(self) -> None:
        if not self._initialized:
            raise McpProtocolError("MCP HTTP listener 只能在初始化后启动")
        if self._listener_task is not None:
            return
        readiness: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._listener_task = asyncio.create_task(
            self._listen_get(readiness),
            name=f"mcp-http-listener-{self._config.server_id}",
        )
        await readiness

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            shutdown_error: McpShutdownFailed | None = None
            client = self._client
            if client is not None and self._session_id is not None:
                try:
                    response = await client.request(
                        "DELETE",
                        self._config.url,
                        headers=self._headers(accept="*/*"),
                        timeout=self._config.shutdown_timeout_seconds,
                    )
                    if not (
                        200 <= response.status_code < 300
                        or response.status_code == 405
                    ):
                        shutdown_error = McpShutdownFailed(
                            f"MCP HTTP server {self._config.server_id} 拒绝关闭 session"
                        )
                except httpx.HTTPError:
                    shutdown_error = McpShutdownFailed(
                        f"MCP HTTP server {self._config.server_id} 关闭 session 失败"
                    )
            if self._listener_task is not None:
                self._listener_task.cancel()
                await asyncio.gather(
                    self._listener_task,
                    return_exceptions=True,
                )
            if client is not None:
                with suppress(Exception):
                    await client.aclose()
            self._closed = True
            if shutdown_error is not None:
                raise shutdown_error

    async def _consume_post_sse(
        self,
        response: httpx.Response,
        request_id: Any,
    ) -> None:
        matched, event_id, retry_seconds = await self._consume_response_stream(
            response,
            request_id,
        )
        while not matched:
            if event_id is None or retry_seconds is None:
                raise McpConnectionLost(
                    f"MCP HTTP server {self._config.server_id} 的 SSE response 提前结束"
                )
            await asyncio.sleep(retry_seconds)
            headers = self._headers(accept="text/event-stream")
            headers["Last-Event-ID"] = event_id
            client = self._require_client()
            try:
                async with client.stream(
                    "GET",
                    self._config.url,
                    headers=headers,
                    timeout=self._long_stream_timeout,
                ) as recovery:
                    if recovery.status_code == 404 and self._session_id is not None:
                        raise McpSessionExpired()
                    if not 200 <= recovery.status_code < 300:
                        raise McpConnectionLost(
                            f"MCP HTTP server {self._config.server_id} 的 SSE 恢复失败"
                        )
                    if self._content_type(recovery) != "text/event-stream":
                        raise McpProtocolError(
                            "MCP HTTP SSE 恢复 Content-Type 无效"
                        )
                    matched, new_event_id, new_retry = (
                        await self._consume_response_stream(
                            recovery,
                            request_id,
                        )
                    )
                    if new_event_id is not None:
                        event_id = new_event_id
                    if new_retry is not None:
                        retry_seconds = new_retry
            except (httpx.TimeoutException, httpx.HTTPError):
                continue

    async def _consume_response_stream(
        self,
        response: httpx.Response,
        request_id: Any,
    ) -> tuple[bool, str | None, float | None]:
        matched = request_id is None
        event_id: str | None = None
        retry_seconds: float | None = None
        try:
            async for event in iter_sse_events(response.aiter_bytes()):
                if event.event_id is not None:
                    event_id = event.event_id
                if event.retry_milliseconds is not None:
                    retry_seconds = event.retry_milliseconds / 1000
                if event.data is None or not event.data:
                    continue
                parsed = decode_json_rpc_message(event.data)
                await self._deliver(event.data)
                if (
                    request_id is not None
                    and isinstance(
                        parsed,
                        (JsonRpcSuccessResponse, JsonRpcErrorResponse),
                    )
                    and parsed.request_id == request_id
                ):
                    matched = True
                    break
        except (httpx.TimeoutException, httpx.HTTPError):
            pass
        return matched, event_id, retry_seconds

    async def _deliver_listener_event(self, data: str) -> None:
        parsed = decode_json_rpc_message(data)
        if isinstance(parsed, (JsonRpcSuccessResponse, JsonRpcErrorResponse)):
            raise McpProtocolError(
                f"MCP HTTP server {self._config.server_id} 的独立 GET listener 收到 response"
            )
        await self._deliver(data)

    async def _listen_get(self, readiness: asyncio.Future[None]) -> None:
        first_attempt = True
        try:
            while not self._closing:
                try:
                    headers = self._headers(accept="text/event-stream")
                    if self._last_get_event_id is not None:
                        headers["Last-Event-ID"] = self._last_get_event_id
                    client = self._require_client()
                    async with client.stream(
                        "GET",
                        self._config.url,
                        headers=headers,
                        timeout=self._long_stream_timeout,
                    ) as response:
                        if response.status_code == 405:
                            self._listener_supported = False
                            if first_attempt and not readiness.done():
                                readiness.set_result(None)
                            return
                        if response.status_code == 404 and self._session_id is not None:
                            raise McpSessionExpired()
                        if not 200 <= response.status_code < 300:
                            raise McpConnectionLost(
                                f"MCP HTTP server {self._config.server_id} 的 GET listener 状态无效"
                            )
                        if self._content_type(response) != "text/event-stream":
                            raise McpProtocolError(
                                "MCP HTTP GET listener Content-Type 无效"
                            )
                        self._listener_supported = True
                        if first_attempt and not readiness.done():
                            readiness.set_result(None)
                        first_attempt = False
                        async for event in iter_sse_events(response.aiter_bytes()):
                            if event.event_id is not None:
                                self._last_get_event_id = event.event_id
                            if event.retry_milliseconds is not None:
                                self._get_retry_seconds = (
                                    event.retry_milliseconds / 1000
                                )
                            if event.data:
                                await self._deliver_listener_event(event.data)
                except (httpx.TimeoutException, httpx.HTTPError) as exc:
                    if first_attempt or self._get_retry_seconds is None:
                        raise McpConnectionLost(
                            f"MCP HTTP server {self._config.server_id} 的 GET listener 网络失败"
                        ) from exc
                if self._closing:
                    return
                if self._get_retry_seconds is None:
                    raise McpConnectionLost(
                        f"MCP HTTP server {self._config.server_id} 的 GET listener 已断开"
                    )
                await asyncio.sleep(self._get_retry_seconds)
        except asyncio.CancelledError:
            if not readiness.done():
                readiness.cancel()
            raise
        except McpError as exc:
            if not readiness.done():
                readiness.set_exception(exc)
            self._fail(exc)
        except (httpx.TimeoutException, httpx.HTTPError):
            error = McpConnectionLost(
                f"MCP HTTP server {self._config.server_id} 的 GET listener 网络失败"
            )
            if not readiness.done():
                readiness.set_exception(error)
            self._fail(error)

    def _validate_common_response(
        self,
        response: httpx.Response,
        message: Mapping[str, Any],
    ) -> None:
        if 300 <= response.status_code < 400:
            raise McpProtocolError("MCP HTTP transport 不允许 redirect")
        if response.status_code == 404 and self._session_id is not None:
            raise McpSessionExpired()
        if not 200 <= response.status_code < 300:
            raise self._request_failure(message, "HTTP 状态无效")

    def _capture_session(self, response: httpx.Response) -> None:
        session_id = response.headers.get("MCP-Session-Id")
        if session_id is not None:
            if not session_id:
                raise McpProtocolError("MCP-Session-Id response header 为空")
            self._session_id = session_id

    def _headers(
        self,
        *,
        accept: str,
        content_type: str | None = None,
    ) -> dict[str, str]:
        headers = dict(self._config.headers)
        headers["Accept"] = accept
        if content_type is not None:
            headers["Content-Type"] = content_type
        if self._initialized:
            headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
        if self._session_id is not None:
            headers["MCP-Session-Id"] = self._session_id
        return headers

    @staticmethod
    def _content_type(response: httpx.Response) -> str:
        return response.headers.get("Content-Type", "").partition(";")[0].strip().lower()

    @staticmethod
    async def _read_limited(response: httpx.Response) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_MCP_MESSAGE_BYTES:
                body.clear()
                raise McpMessageTooLarge()
        return bytes(body)

    async def _deliver(self, payload: bytes | str) -> None:
        if self._on_message is None:
            raise McpConnectionLost("MCP HTTP 入站 handler 未连接")
        await self._on_message(payload)

    def _request_failure(
        self,
        message: Mapping[str, Any],
        reason: str,
    ) -> McpError:
        if message.get("method") == "initialize":
            return McpConnectFailed(
                f"MCP HTTP server {self._config.server_id} 初始化{reason}"
            )
        return McpConnectionLost(
            f"MCP HTTP server {self._config.server_id} {reason}"
        )

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise McpConnectionLost("MCP HTTP client 未连接")
        return self._client

    def _fail(self, error: McpError) -> None:
        if self._failure is not None or self._closing:
            return
        self._failure = error
        if self._on_close is not None:
            self._on_close(error)
