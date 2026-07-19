"""Newline-delimited MCP transport over a long-lived child process."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from mewcode_agent.mcp.models import (
    MAX_MCP_MESSAGE_BYTES,
    MAX_MCP_STDERR_BYTES,
    MCP_PROTOCOL_VERSION,
    McpConnectFailed,
    McpConnectionLost,
    McpError,
    McpMessageTooLarge,
    McpProtocolError,
    McpShutdownFailed,
    StdioServerConfig,
)
from mewcode_agent.mcp.protocol import encode_json_rpc_message
from mewcode_agent.mcp.transports.base import (
    CloseHandler,
    InboundMessageHandler,
    McpTransport,
)


class StdioTransport(McpTransport):
    """Run one configured MCP server without invoking a shell."""

    def __init__(self, config: StdioServerConfig) -> None:
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._on_message: InboundMessageHandler | None = None
        self._on_close: CloseHandler | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._writer_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._stderr_buffer = bytearray()
        self._failure: McpError | None = None
        self._closing = False
        self._closed = False

    @property
    def failure(self) -> McpError | None:
        return self._failure

    @property
    def stderr_size(self) -> int:
        return len(self._stderr_buffer)

    @property
    def stderr_tail(self) -> str:
        text = self._stderr_buffer.decode("utf-8", errors="replace")
        for secret in self._config.env.values():
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    async def connect(
        self,
        on_message: InboundMessageHandler,
        on_close: CloseHandler,
    ) -> None:
        if self._process is not None or self._closed:
            raise McpConnectFailed(
                f"MCP server {self._config.server_id} 的 stdio transport 状态无效"
            )
        self._on_message = on_message
        self._on_close = on_close
        try:
            self._process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    self._config.command,
                    *self._config.args,
                    cwd=self._config.cwd,
                    env=dict(self._config.env),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=MAX_MCP_MESSAGE_BYTES + 2,
                ),
                timeout=self._config.connect_timeout_seconds,
            )
        except (OSError, TimeoutError, ValueError) as exc:
            raise McpConnectFailed(
                f"无法启动 MCP stdio server {self._config.server_id}"
            ) from exc
        if (
            self._process.stdin is None
            or self._process.stdout is None
            or self._process.stderr is None
        ):
            await self._cleanup_incomplete_process()
            raise McpConnectFailed(
                f"MCP stdio server {self._config.server_id} 缺少 pipe"
            )
        self._reader_task = asyncio.create_task(
            self._read_stdout(),
            name=f"mcp-stdio-reader-{self._config.server_id}",
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(),
            name=f"mcp-stdio-stderr-{self._config.server_id}",
        )

    async def send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if (
            process is None
            or process.stdin is None
            or process.returncode is not None
            or self._closing
        ):
            error = self._failure or McpConnectionLost(
                f"MCP stdio server {self._config.server_id} 未连接"
            )
            raise error
        payload = encode_json_rpc_message(message) + b"\n"
        try:
            async with self._writer_lock:
                process.stdin.write(payload)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            error = McpConnectionLost(
                f"MCP stdio server {self._config.server_id} 写入失败"
            )
            self._fail(error)
            raise error from exc

    def mark_initialized(self, protocol_version: str) -> None:
        if protocol_version != MCP_PROTOCOL_VERSION:
            raise ValueError("stdio transport 收到不支持的 MCP 协议版本")

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            forced = await self._stop_process()
            await self._finish_tasks()
            self._closed = True
            if forced:
                raise McpShutdownFailed(
                    f"MCP stdio server {self._config.server_id} 需要强制终止"
                )

    async def _read_stdout(self) -> None:
        process = self._process
        handler = self._on_message
        assert process is not None and process.stdout is not None
        assert handler is not None
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError as exc:
                    raise McpMessageTooLarge() from exc
                if not line:
                    if not self._closing:
                        self._fail(
                            McpConnectionLost(
                                f"MCP stdio server {self._config.server_id} 已退出"
                            )
                        )
                    return
                if not line.endswith(b"\n"):
                    raise McpProtocolError("MCP stdio 消息缺少换行分隔符")
                payload = line[:-1]
                if payload.endswith(b"\r"):
                    payload = payload[:-1]
                if not payload.strip():
                    continue
                if len(payload) > MAX_MCP_MESSAGE_BYTES:
                    raise McpMessageTooLarge()
                await handler(payload)
        except asyncio.CancelledError:
            raise
        except McpError as exc:
            self._fail(exc)
        except Exception:
            self._fail(McpProtocolError("MCP stdio 入站消息处理失败"))

    async def _drain_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            while True:
                chunk = await process.stderr.read(64 * 1024)
                if not chunk:
                    return
                self._stderr_buffer.extend(chunk)
                overflow = len(self._stderr_buffer) - MAX_MCP_STDERR_BYTES
                if overflow > 0:
                    del self._stderr_buffer[:overflow]
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _fail(self, error: McpError) -> None:
        if self._failure is not None or self._closing:
            return
        self._failure = error
        if self._on_close is not None:
            self._on_close(error)

    async def _stop_process(self) -> bool:
        process = self._process
        if process is None:
            return False
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionError, OSError):
                await process.stdin.wait_closed()
        if process.returncode is not None:
            await process.wait()
            return False
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self._config.shutdown_timeout_seconds,
            )
            return False
        except TimeoutError:
            pass

        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self._config.shutdown_timeout_seconds,
            )
            return True
        except TimeoutError:
            pass

        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return True

    async def _finish_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._reader_task, self._stderr_task)
            if task is not None and task is not current
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cleanup_incomplete_process(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()
