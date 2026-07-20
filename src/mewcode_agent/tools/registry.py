"""Tool registration, provider schemas, timeout, and safe execution."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import json
from pathlib import Path
from typing import Any, Literal

from mewcode_agent.compaction.artifacts import ContextArtifactStore

from mewcode_agent.security.boundary import SecurityBoundary
from mewcode_agent.security.models import SecurityRequest
from mewcode_agent.security.path_sandbox import PathSandbox, PathSandboxError
from mewcode_agent.tools.base import Tool, ToolExecutionError, ToolResult
from mewcode_agent.tools.edit_file import EditFileTool
from mewcode_agent.tools.file_state_cache import FileStateCache
from mewcode_agent.tools.find_files import FindFilesTool
from mewcode_agent.tools.read_file import ReadFileTool
from mewcode_agent.tools.read_context_artifact import ReadContextArtifactTool
from mewcode_agent.tools.run_command import RunCommandTool
from mewcode_agent.tools.search_code import SearchCodeTool
from mewcode_agent.tools.write_file import WriteFileTool

ToolProtocol = Literal["openai", "anthropic"]


class ToolRegistry:
    """Own all available tools and execute them by their exact names."""

    def __init__(
        self,
        *,
        security_boundary: SecurityBoundary | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._mcp_tools_by_server: dict[str, tuple[Tool, ...]] = {}
        self._security_boundary = security_boundary

    @property
    def security_boundary(self) -> SecurityBoundary | None:
        return self._security_boundary

    def register(self, tool: Tool) -> None:
        if tool.name.startswith("mcp_"):
            raise ValueError("mcp_ 工具名前缀由 MCP 子系统保留")
        if tool.name in self._tools:
            raise ValueError(f"工具已注册: {tool.name}")
        self._tools[tool.name] = tool

    def replace_mcp_tools(
        self,
        server_id: str,
        tools: Iterable[Tool],
    ) -> None:
        """Atomically replace one server's MCP tools and preserve all others."""

        if not isinstance(server_id, str) or not server_id:
            raise ValueError("MCP server_id 必须是非空字符串")
        replacement = tuple(tools)
        expected_prefix = f"mcp_{server_id}_"
        replacement_names: set[str] = set()
        for tool in replacement:
            if not isinstance(tool, Tool):
                raise TypeError("MCP 工具必须实现 Tool")
            if not tool.name.startswith(expected_prefix):
                raise ValueError("MCP 工具名与 server_id 不匹配")
            if tool.name in replacement_names:
                raise ValueError(f"MCP 工具别名冲突: {tool.name}")
            replacement_names.add(tool.name)

        old_mcp_names = {
            tool.name
            for group in self._mcp_tools_by_server.values()
            for tool in group
        }
        base_tools = [
            tool
            for name, tool in self._tools.items()
            if name not in old_mcp_names
        ]
        groups = dict(self._mcp_tools_by_server)
        if replacement:
            groups[server_id] = replacement
        else:
            groups.pop(server_id, None)

        rebuilt: dict[str, Tool] = {}
        for tool in base_tools:
            if tool.name in rebuilt:
                raise ValueError(f"工具已注册: {tool.name}")
            rebuilt[tool.name] = tool
        for grouped_server_id in sorted(groups):
            for tool in groups[grouped_server_id]:
                if tool.name in rebuilt:
                    raise ValueError(f"MCP 工具别名冲突: {tool.name}")
                rebuilt[tool.name] = tool
        self._tools = rebuilt
        self._mcp_tools_by_server = groups

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
            if self._security_boundary is not None:
                boundary_decision = self._security_boundary.evaluate(
                    SecurityRequest(
                        "registry-direct",
                        name,
                        tool.category,
                        arguments,
                        self._security_boundary.path_sandbox.working_directory,
                    )
                )
                if boundary_decision is not None:
                    raise ToolExecutionError(
                        "security_denied",
                        (
                            "工具调用被安全边界拒绝: "
                            f"{boundary_decision.reason_code}"
                        ),
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
        except PathSandboxError:
            return ToolResult(
                tool_name=name,
                success=False,
                error_code="security_denied",
                error_message="工具路径超出允许目录",
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


def create_core_registry(
    *,
    working_directory: Path | None = None,
    artifact_store: ContextArtifactStore | None = None,
) -> ToolRegistry:
    path_sandbox = PathSandbox(working_directory or Path.cwd())
    security_boundary = SecurityBoundary(path_sandbox)
    registry = ToolRegistry(security_boundary=security_boundary)
    file_state_cache = FileStateCache()
    tools = (
        ReadFileTool(file_state_cache, path_sandbox),
        WriteFileTool(file_state_cache, path_sandbox),
        EditFileTool(file_state_cache, path_sandbox),
        RunCommandTool(path_sandbox),
        FindFilesTool(path_sandbox),
        SearchCodeTool(path_sandbox),
    )
    for tool in tools:
        registry.register(tool)
    if artifact_store is not None:
        registry.register(ReadContextArtifactTool(artifact_store))
    return registry
