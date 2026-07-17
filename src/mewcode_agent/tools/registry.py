"""Tool registration, provider schemas, timeout, and safe execution."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from mewcode_agent.tools.base import Tool, ToolExecutionError, ToolResult
from mewcode_agent.tools.edit_file import EditFileTool
from mewcode_agent.tools.file_state_cache import FileStateCache
from mewcode_agent.tools.find_files import FindFilesTool
from mewcode_agent.tools.read_file import ReadFileTool
from mewcode_agent.tools.run_command import RunCommandTool
from mewcode_agent.tools.search_code import SearchCodeTool
from mewcode_agent.tools.write_file import WriteFileTool

ToolProtocol = Literal["openai", "anthropic"]


class ToolRegistry:
    """Own all available tools and execute them by their exact names."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具已注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def api_tools(self, protocol: ToolProtocol) -> list[dict[str, Any]]:
        if protocol == "openai":
            return [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in self._tools.values()
            ]
        if protocol == "anthropic":
            return [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in self._tools.values()
            ]
        raise ValueError(f"不支持的工具协议: {protocol}")

    async def execute(self, name: str, arguments_json: str) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(
                tool_name=name,
                success=False,
                error_code="tool_not_found",
                error_message=f"未注册工具: {name}",
            )

        try:
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                raise ToolExecutionError(
                    "invalid_arguments",
                    "工具参数必须是 JSON 对象",
                )
            data = await asyncio.wait_for(
                tool.execute(arguments),
                timeout=tool.timeout_seconds,
            )
            return ToolResult(tool_name=name, success=True, data=data)
        except json.JSONDecodeError as exc:
            return ToolResult(
                tool_name=name,
                success=False,
                error_code="invalid_json",
                error_message=f"工具参数不是有效的 JSON: {exc.msg}",
            )
        except TimeoutError:
            return ToolResult(
                tool_name=name,
                success=False,
                error_code="timeout",
                error_message=f"工具执行超过 {tool.timeout_seconds:g} 秒",
            )
        except ToolExecutionError as exc:
            return ToolResult(
                tool_name=name,
                success=False,
                data=exc.details,
                error_code=exc.code,
                error_message=exc.message,
            )
        except FileNotFoundError as exc:
            return ToolResult(
                tool_name=name,
                success=False,
                error_code="file_not_found",
                error_message=f"文件或目录不存在: {exc.filename}",
            )
        except PermissionError as exc:
            return ToolResult(
                tool_name=name,
                success=False,
                error_code="permission_denied",
                error_message=f"没有权限访问: {exc.filename}",
            )
        except UnicodeError:
            return ToolResult(
                tool_name=name,
                success=False,
                error_code="invalid_encoding",
                error_message="文件不是有效的 UTF-8 文本",
            )
        except OSError as exc:
            return ToolResult(
                tool_name=name,
                success=False,
                error_code="os_error",
                error_message=f"操作系统错误: {exc}",
            )
        except Exception:
            return ToolResult(
                tool_name=name,
                success=False,
                error_code="internal_error",
                error_message="工具执行发生未预期错误",
            )


def create_core_registry() -> ToolRegistry:
    registry = ToolRegistry()
    file_state_cache = FileStateCache()
    tools = (
        ReadFileTool(file_state_cache),
        WriteFileTool(file_state_cache),
        EditFileTool(file_state_cache),
        RunCommandTool(),
        FindFilesTool(),
        SearchCodeTool(),
    )
    for tool in tools:
        registry.register(tool)
    return registry
