"""System command execution tool."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from mewcode_agent.security.path_sandbox import PathSandbox
from mewcode_agent.tools._paths import expand_path
from mewcode_agent.tools.base import Tool, ToolExecutionError, validate_arguments


class RunCommandTool(Tool):
    category = "command"
    name = "run_command"
    description = (
        "在系统命令解释器中执行命令并返回退出码、标准输出和标准错误。"
        "Windows 使用 PowerShell，其他系统使用 /bin/sh。"
        "cwd 规范化结果必须位于启动工作目录内；已知危险命令会在执行前被拒绝。"
        "read_file、find_files 或 search_code 能完成文件读取、文件发现或代码搜索时，"
        "必须使用对应专用工具，不要用本工具替代。"
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

    def __init__(self, path_sandbox: PathSandbox | None = None) -> None:
        self._path_sandbox = path_sandbox

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
        default_cwd = (
            self._path_sandbox.working_directory
            if self._path_sandbox is not None
            else Path.cwd()
        )
        raw_cwd = arguments.get("cwd", str(default_cwd))
        cwd = (
            self._path_sandbox.resolve(raw_cwd)
            if self._path_sandbox is not None
            else expand_path(raw_cwd)
        )

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
