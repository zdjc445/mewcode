from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import sys
from typing import Any

import pytest

from mewcode_agent.mcp import (
    MAX_MCP_STDERR_BYTES,
    McpError,
    McpProtocolError,
    McpShutdownFailed,
    StdioServerConfig,
    decode_json_rpc_message,
)
from mewcode_agent.mcp.transports import StdioTransport


def _child_environment(**extra: str) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT")
        if key in os.environ
    }
    environment["PYTHONUNBUFFERED"] = "1"
    environment.update(extra)
    return environment


def _config(
    tmp_path: Path,
    script: Path,
    *,
    environment: dict[str, str] | None = None,
    shutdown_timeout: float = 1,
) -> StdioServerConfig:
    return StdioServerConfig(
        server_id="fake_stdio",
        required=True,
        command=sys.executable,
        args=(str(script),),
        cwd=tmp_path,
        env=environment or _child_environment(),
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        shutdown_timeout_seconds=shutdown_timeout,
        tool_categories={},
    )


def _write_script(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(source, encoding="utf-8")
    return script


async def _wait_until(predicate: Any, *, timeout: float = 2) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=timeout)


async def test_stdio_round_trip_and_clean_eof_shutdown(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path,
        """import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    response = {"jsonrpc": "2.0", "id": message["id"], "result": message["params"]}
    print(json.dumps(response, separators=(",", ":")), flush=True)
""",
    )
    transport = StdioTransport(_config(tmp_path, script))
    received: list[Any] = []
    closed: list[McpError] = []

    async def on_message(payload: bytes | str) -> None:
        received.append(decode_json_rpc_message(payload))

    await transport.connect(on_message, closed.append)
    transport.mark_initialized("2025-11-25")
    await transport.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "echo",
            "params": {"text": "line one\nline two"},
        }
    )
    await _wait_until(lambda: len(received) == 1)
    await transport.close()

    assert received[0].result == {"text": "line one\nline two"}
    assert transport.returncode == 0
    assert closed == []


async def test_stdio_drains_and_bounds_stderr_without_exposing_env_secret(
    tmp_path: Path,
) -> None:
    secret = "stdio-secret-value"
    script = _write_script(
        tmp_path,
        f"""import json
import sys

sys.stderr.write("x" * 300000)
sys.stderr.write({secret!r})
sys.stderr.flush()
for line in sys.stdin:
    message = json.loads(line)
    print(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "result": {{}}}}), flush=True)
""",
    )
    environment = _child_environment(MCP_SECRET=secret)
    transport = StdioTransport(
        _config(tmp_path, script, environment=environment)
    )
    received = asyncio.Event()

    async def on_message(payload: bytes | str) -> None:
        decode_json_rpc_message(payload)
        received.set()

    await transport.connect(on_message, lambda error: None)
    try:
        await transport.send(
            {"jsonrpc": "2.0", "id": 1, "method": "test", "params": {}}
        )
        await asyncio.wait_for(received.wait(), timeout=2)
        await _wait_until(lambda: transport.stderr_size > 0)
        assert transport.stderr_size <= MAX_MCP_STDERR_BYTES
        assert secret not in transport.stderr_tail
        assert "[REDACTED]" in transport.stderr_tail
    finally:
        await transport.close()


async def test_stdio_invalid_stdout_fails_transport(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path,
        """import sys

print("this is not JSON-RPC", flush=True)
for _line in sys.stdin:
    pass
""",
    )
    transport = StdioTransport(_config(tmp_path, script))
    closed: list[McpError] = []

    async def on_message(payload: bytes | str) -> None:
        decode_json_rpc_message(payload)

    await transport.connect(on_message, closed.append)
    try:
        await _wait_until(lambda: bool(closed))
        assert isinstance(closed[0], McpProtocolError)
        assert transport.failure is closed[0]
    finally:
        await transport.close()


async def test_stdio_force_termination_is_reported_after_reaping(
    tmp_path: Path,
) -> None:
    script = _write_script(
        tmp_path,
        """import sys
import time

sys.stdin.read()
time.sleep(60)
""",
    )
    transport = StdioTransport(
        _config(tmp_path, script, shutdown_timeout=0.02)
    )
    await transport.connect(lambda payload: asyncio.sleep(0), lambda error: None)

    with pytest.raises(McpShutdownFailed):
        await transport.close()

    assert transport.returncode is not None
    with suppress(McpShutdownFailed):
        await transport.close()
