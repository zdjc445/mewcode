from __future__ import annotations

import os
from pathlib import Path
import sys

from mewcode_agent.mcp import (
    McpConfiguration,
    McpConnectionManager,
    StdioServerConfig,
    local_tool_name,
)
from mewcode_agent.mcp.transports import StdioTransport
from mewcode_agent.tools import ToolRegistry


def _child_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT")
        if key in os.environ
    }
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


async def test_stdio_server_runs_through_manager_registry_and_adapter(
    tmp_path: Path,
) -> None:
    server_script = tmp_path / "end_to_end_mcp_server.py"
    server_script.write_text(
        """import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "end-to-end", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "Echo.Tool",
                    "description": "Echo exact text",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "outputSchema": {
                        "type": "object",
                        "properties": {"echo": {"type": "string"}},
                        "required": ["echo"],
                    },
                }
            ]
        }
    elif method == "tools/call":
        text = message["params"]["arguments"]["text"]
        result = {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"echo": text},
        }
    else:
        continue
    print(
        json.dumps(
            {"jsonrpc": "2.0", "id": message["id"], "result": result},
            separators=(",", ":"),
        ),
        flush=True,
    )
""",
        encoding="utf-8",
    )
    config = StdioServerConfig(
        server_id="end_to_end",
        required=True,
        command=sys.executable,
        args=(str(server_script),),
        cwd=tmp_path,
        env=_child_environment(),
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        shutdown_timeout_seconds=1,
        tool_categories={"Echo.Tool": "read"},
    )
    registry = ToolRegistry()
    transports: list[StdioTransport] = []

    def create_stdio(_config: object) -> StdioTransport:
        transport = StdioTransport(config)
        transports.append(transport)
        return transport

    manager = McpConnectionManager(
        McpConfiguration((config,)),
        registry,
        transport_factory=create_stdio,
    )
    await manager.activate_all()
    alias = local_tool_name("end_to_end", "Echo.Tool")

    try:
        tool = registry.get(alias)
        assert tool is not None
        assert tool.category == "read"
        result = await registry.execute(alias, '{"text":"精确回显"}')
        assert result.success is True
        assert result.data == {
            "server_id": "end_to_end",
            "remote_tool_name": "Echo.Tool",
            "content": [{"type": "text", "text": "精确回显"}],
            "structured_content": {"echo": "精确回显"},
        }
    finally:
        await manager.close()

    assert transports[0].returncode == 0
