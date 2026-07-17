"""System command execution tool."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from mewcode_agent.tools._paths import expand_path
from mewcode_agent.tools.base import Tool, ToolExecutionError, validate_arguments


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "在系统命令解释器中执行命令并返回退出码、标准输出和标准错误。"
        "Windows 使用 PowerShell，其他系统使用 /bin/sh。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "cwd": {
                "type": "string",
                "description": "可选工作目录；默认使用当前工作目录",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_arguments(
            arguments,
            required={"command": str},
            optional={"cwd": str},
        )
        command = arguments["command"]
        if not command.strip():
            raise ToolExecutionError(
                "invalid_arguments",
                "参数 command 不能为空",
            )
        cwd = expand_path(arguments["cwd"]) if "cwd" in arguments else Path.cwd()

        if os.name == "nt":
            process = await asyncio.create_subprocess_exec(
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-lc",
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        try:
            stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise

        result = {
            "cwd": str(cwd.resolve()),
            "exit_code": process.returncode,
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        }
        if process.returncode != 0:
            raise ToolExecutionError(
                "command_failed",
                f"命令执行失败，退出码为 {process.returncode}",
                details=result,
            )
        return result

