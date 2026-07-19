"""MCP lifecycle, capability negotiation, tool discovery, and invocation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from importlib import metadata
from typing import Any, TypeAlias, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable

from mewcode_agent.mcp.models import (
    MCP_PROTOCOL_VERSION,
    McpConnectionLost,
    McpDiagnostic,
    McpInvalidToolResult,
    McpProtocolError,
    McpServerConfig,
    McpServerInfo,
    McpServerSnapshot,
    McpSessionExpired,
    McpToolCallResult,
    McpToolDefinition,
    McpToolNotFound,
    McpToolsCapabilityMissing,
    UnsupportedMcpVersion,
    freeze_json,
    thaw_json,
)
from mewcode_agent.mcp.protocol import JsonRpcSession
from mewcode_agent.mcp.transports.base import McpTransport

MAX_MCP_TOOL_PAGES = 100
MAX_MCP_TOOLS_PER_SERVER = 512

ToolsChangedHandler: TypeAlias = Callable[[], Awaitable[None]]


def _expect_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise McpProtocolError(f"{location} 必须是 object")
    if any(not isinstance(key, str) for key in value):
        raise McpProtocolError(f"{location} 的字段名必须是字符串")
    return cast(Mapping[str, Any], value)


def _schema_validator(
    schema: Mapping[str, Any],
    *,
    location: str,
) -> type[Any]:
    dialect = schema.get("$schema")
    if dialect is None:
        validator_class: type[Any] | None = Draft202012Validator
    else:
        if not isinstance(dialect, str) or not dialect:
            raise McpProtocolError(f"{location} 的 $schema 必须是非空字符串")
        validator_class = validator_for(schema, default=None)
        if validator_class is None:
            raise McpProtocolError(f"{location} 使用了不支持的 JSON Schema dialect")
    try:
        validator_class.check_schema(schema)
    except SchemaError as exc:
        raise McpProtocolError(f"{location} 不是有效 JSON Schema") from exc
    return validator_class


def _validate_tool_schema(
    schema: Mapping[str, Any],
    *,
    location: str,
) -> type[Any]:
    if schema.get("type") != "object":
        raise McpProtocolError(f'{location} 的根 type 必须精确为 "object"')
    return _schema_validator(schema, location=location)


class McpClient:
    """Own one negotiated MCP session and its atomic remote-tool snapshot."""

    def __init__(
        self,
        config: McpServerConfig,
        transport: McpTransport,
    ) -> None:
        self._config = config
        self._transport = transport
        self._session = self._new_session()
        self._server_info: McpServerInfo | None = None
        self._capabilities: Mapping[str, Any] = {}
        self._instructions: str | None = None
        self._list_changed = False
        self._tools: tuple[McpToolDefinition, ...] = ()
        self._tools_by_name: Mapping[str, McpToolDefinition] = {}
        self._diagnostics: tuple[McpDiagnostic, ...] = ()
        self._initialized = False
        self._connected = False
        self._closed = False
        self._discovery_lock = asyncio.Lock()
        self._tools_changed_handler: ToolsChangedHandler | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    @property
    def config(self) -> McpServerConfig:
        return self._config

    @property
    def server_id(self) -> str:
        return self._config.server_id

    @property
    def connected(self) -> bool:
        return self._connected and not self._closed

    @property
    def tools(self) -> tuple[McpToolDefinition, ...]:
        return self._tools

    @property
    def diagnostics(self) -> tuple[McpDiagnostic, ...]:
        return self._diagnostics

    @property
    def list_changed_supported(self) -> bool:
        return self._list_changed

    def set_tools_changed_handler(
        self,
        handler: ToolsChangedHandler | None,
    ) -> None:
        self._tools_changed_handler = handler

    async def connect(self) -> McpServerSnapshot:
        if self._connected or self._closed:
            raise McpProtocolError(
                f"MCP client {self.server_id} 的连接状态无效"
            )
        await self._transport.connect(
            self._handle_inbound_message,
            self._handle_transport_close,
        )
        try:
            try:
                return await self._initialize_session()
            except McpSessionExpired:
                await self._reset_logical_session()
                return await self._initialize_session()
        except BaseException:
            self._session.close(McpConnectionLost("MCP 初始化失败"))
            with suppress(BaseException):
                await self._transport.close()
            self._closed = True
            raise

    async def reinitialize(self) -> McpServerSnapshot:
        if self._closed:
            raise McpConnectionLost("MCP client 已关闭")
        if (
            self._refresh_task is not None
            and self._refresh_task is not asyncio.current_task()
        ):
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
            self._refresh_task = None
        await self._reset_logical_session()
        return await self._initialize_session()

    async def discover_tools(self) -> tuple[McpToolDefinition, ...]:
        if not self._initialized:
            raise McpConnectionLost("MCP client 尚未初始化")
        session = self._session
        try:
            async with self._discovery_lock:
                tools, diagnostics, discovered_names = (
                    await self._read_all_tool_pages()
                )
                missing_categories = sorted(
                    set(self._config.tool_categories) - discovered_names
                )
                if missing_categories:
                    names = ", ".join(
                        repr(name) for name in missing_categories
                    )
                    raise McpProtocolError(
                        "MCP tool_categories 包含未发现的远端工具: "
                        f"{names}"
                    )
                ordered = tuple(sorted(tools, key=lambda item: item.name))
                self._tools = ordered
                self._tools_by_name = {tool.name: tool for tool in ordered}
                self._diagnostics = tuple(diagnostics)
                return ordered
        except McpSessionExpired:
            if self._session is session:
                self._connected = False
            raise

    async def call_tool(
        self,
        remote_tool_name: str,
        arguments: Mapping[str, Any],
    ) -> McpToolCallResult:
        if not self.connected:
            raise McpConnectionLost("MCP client 未连接")
        definition = self._tools_by_name.get(remote_tool_name)
        if definition is None:
            raise McpToolNotFound()
        if not isinstance(arguments, Mapping) or any(
            not isinstance(key, str) for key in arguments
        ):
            raise ValueError("MCP tool arguments 必须是 JSON object")
        session = self._session
        try:
            result = await session.request(
                "tools/call",
                {
                    "name": remote_tool_name,
                    "arguments": dict(arguments),
                },
                timeout_seconds=None,
            )
        except McpSessionExpired:
            if self._session is session:
                self._connected = False
            raise
        return self._parse_tool_result(result, definition)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
            self._refresh_task = None
        self._session.close(McpConnectionLost("MCP client 已关闭"))
        await self._transport.close()

    def snapshot(self) -> McpServerSnapshot:
        if self._server_info is None:
            raise McpConnectionLost("MCP client 尚未初始化")
        return McpServerSnapshot(
            server_id=self.server_id,
            server_info=self._server_info,
            capabilities=self._capabilities,
            instructions=self._instructions,
            tools=self._tools,
            list_changed=self._list_changed,
            diagnostics=self._diagnostics,
        )

    def _new_session(self) -> JsonRpcSession:
        session = JsonRpcSession(self._transport.send)
        session.set_notification_handler(
            "notifications/tools/list_changed",
            self._on_tools_list_changed,
        )
        return session

    async def _reset_logical_session(self) -> None:
        self._session.close(McpConnectionLost("MCP session 已重置"))
        await self._transport.reset_session()
        self._session = self._new_session()
        self._initialized = False
        self._connected = False

    async def _initialize_session(self) -> McpServerSnapshot:
        try:
            client_version = metadata.version("mewcode-agent")
        except metadata.PackageNotFoundError as exc:
            raise McpProtocolError("无法读取 mewcode-agent 安装版本") from exc
        result = await self._session.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "mewcode-agent",
                    "version": client_version,
                },
            },
            timeout_seconds=self._config.request_timeout_seconds,
        )
        data = _expect_mapping(result, "initialize result")
        if data.get("protocolVersion") != MCP_PROTOCOL_VERSION:
            raise UnsupportedMcpVersion()
        capabilities = _expect_mapping(
            data.get("capabilities"),
            "initialize result.capabilities",
        )
        tools_capability = capabilities.get("tools")
        if not isinstance(tools_capability, Mapping):
            raise McpToolsCapabilityMissing()
        list_changed = tools_capability.get("listChanged", False)
        if type(list_changed) is not bool:
            raise McpProtocolError(
                "initialize result.capabilities.tools.listChanged 必须是布尔值"
            )
        raw_server_info = _expect_mapping(
            data.get("serverInfo"),
            "initialize result.serverInfo",
        )
        server_name = raw_server_info.get("name")
        server_version = raw_server_info.get("version")
        if not isinstance(server_name, str) or not server_name:
            raise McpProtocolError("initialize result.serverInfo.name 必须是非空字符串")
        if not isinstance(server_version, str) or not server_version:
            raise McpProtocolError(
                "initialize result.serverInfo.version 必须是非空字符串"
            )
        instructions = data.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise McpProtocolError("initialize result.instructions 必须是字符串")

        self._server_info = McpServerInfo(server_name, server_version)
        self._capabilities = freeze_json(capabilities)
        self._instructions = instructions
        self._list_changed = list_changed
        self._transport.mark_initialized(MCP_PROTOCOL_VERSION)
        await self._session.notify("notifications/initialized")
        self._initialized = True
        await self._transport.start_listener()
        await self.discover_tools()
        self._connected = True
        return self.snapshot()

    async def _read_all_tool_pages(
        self,
    ) -> tuple[list[McpToolDefinition], list[McpDiagnostic], set[str]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        discovered_names: set[str] = set()
        tools: list[McpToolDefinition] = []
        diagnostics: list[McpDiagnostic] = []

        for _page_number in range(1, MAX_MCP_TOOL_PAGES + 1):
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            raw_result = await self._session.request(
                "tools/list",
                params,
                timeout_seconds=self._config.request_timeout_seconds,
            )
            result = _expect_mapping(raw_result, "tools/list result")
            raw_tools = result.get("tools")
            if not isinstance(raw_tools, list):
                raise McpProtocolError("tools/list result.tools 必须是 list")
            for raw_tool in raw_tools:
                parsed, diagnostic, discovered_name = self._parse_tool(raw_tool)
                if discovered_name in discovered_names:
                    raise McpProtocolError("tools/list 返回了重复的远端工具名")
                discovered_names.add(discovered_name)
                if len(discovered_names) > MAX_MCP_TOOLS_PER_SERVER:
                    raise McpProtocolError("MCP server 的工具数量超过 512")
                if parsed is not None:
                    tools.append(parsed)
                if diagnostic is not None:
                    diagnostics.append(diagnostic)

            if "nextCursor" not in result:
                return tools, diagnostics, discovered_names
            next_cursor = result["nextCursor"]
            if not isinstance(next_cursor, str) or not next_cursor:
                raise McpProtocolError(
                    "tools/list result.nextCursor 必须是非空字符串"
                )
            if next_cursor in seen_cursors:
                raise McpProtocolError("tools/list nextCursor 出现循环")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise McpProtocolError("tools/list 分页超过 100 页")

    def _parse_tool(
        self,
        value: Any,
    ) -> tuple[McpToolDefinition | None, McpDiagnostic | None, str]:
        data = _expect_mapping(value, "tools/list tool")
        name = data.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise McpProtocolError("MCP 远端工具 name 长度必须为 1 到 128")
        input_schema = _expect_mapping(
            data.get("inputSchema"),
            "MCP 远端工具 inputSchema",
        )
        _validate_tool_schema(
            input_schema,
            location="MCP 远端工具 inputSchema",
        )

        output_schema: Mapping[str, Any] | None = None
        if "outputSchema" in data:
            output_schema = _expect_mapping(
                data["outputSchema"],
                "MCP 远端工具 outputSchema",
            )
            _validate_tool_schema(
                output_schema,
                location="MCP 远端工具 outputSchema",
            )
        description = data.get("description")
        if description is None:
            description = (
                f"MCP server {self.server_id} 的远端工具 {name} 未提供描述"
            )
        elif not isinstance(description, str):
            raise McpProtocolError("MCP 远端工具 description 必须是字符串")

        annotations: Mapping[str, Any] = {}
        if "annotations" in data:
            annotations = _expect_mapping(
                data["annotations"],
                "MCP 远端工具 annotations",
            )
        task_support: str | None = None
        if "execution" in data:
            execution = _expect_mapping(
                data["execution"],
                "MCP 远端工具 execution",
            )
            task_support = execution.get("taskSupport")
            if task_support not in (
                None,
                "forbidden",
                "optional",
                "required",
            ):
                raise McpProtocolError(
                    "MCP 远端工具 execution.taskSupport 无效"
                )
        if task_support == "required":
            return (
                None,
                McpDiagnostic(
                    server_id=self.server_id,
                    code="mcp_task_required_unsupported",
                    message=f"跳过需要 MCP Tasks 的远端工具 {name!r}",
                ),
                name,
            )
        return (
            McpToolDefinition(
                name=name,
                description=description,
                input_schema=input_schema,
                output_schema=output_schema,
                annotations=annotations,
                task_support=cast(Any, task_support),
            ),
            None,
            name,
        )

    def _parse_tool_result(
        self,
        value: Any,
        definition: McpToolDefinition,
    ) -> McpToolCallResult:
        result = _expect_mapping(value, "tools/call result")
        raw_content = result.get("content")
        if not isinstance(raw_content, list):
            raise McpInvalidToolResult("tools/call result.content 必须是 list")
        content: list[Mapping[str, Any]] = []
        supported_types = {"text", "image", "audio", "resource_link", "resource"}
        for item in raw_content:
            try:
                content_item = _expect_mapping(item, "tools/call content item")
            except McpProtocolError as exc:
                raise McpInvalidToolResult(
                    "tools/call content item 必须是 object"
                ) from exc
            if content_item.get("type") not in supported_types:
                raise McpInvalidToolResult(
                    "tools/call content item.type 不受支持"
                )
            content.append(content_item)
        is_error = result.get("isError", False)
        if type(is_error) is not bool:
            raise McpInvalidToolResult("tools/call result.isError 必须是布尔值")

        structured_content: Mapping[str, Any] | None = None
        if "structuredContent" in result:
            try:
                structured_content = _expect_mapping(
                    result["structuredContent"],
                    "tools/call result.structuredContent",
                )
            except McpProtocolError as exc:
                raise McpInvalidToolResult(
                    "tools/call result.structuredContent 必须是 object"
                ) from exc
        meta: Mapping[str, Any] = {}
        if "_meta" in result:
            try:
                meta = _expect_mapping(result["_meta"], "tools/call result._meta")
            except McpProtocolError as exc:
                raise McpInvalidToolResult(
                    "tools/call result._meta 必须是 object"
                ) from exc

        if not is_error and definition.output_schema is not None:
            if structured_content is None:
                raise McpInvalidToolResult(
                    "tools/call 成功结果缺少 structuredContent"
                )
            schema = thaw_json(definition.output_schema)
            validator_class = _validate_tool_schema(
                schema,
                location="MCP 远端工具 outputSchema",
            )
            try:
                validator_class(schema, registry=Registry()).validate(
                    thaw_json(structured_content)
                )
            except (ValidationError, Unresolvable) as exc:
                raise McpInvalidToolResult(
                    "tools/call structuredContent 不符合 outputSchema"
                ) from exc
        return McpToolCallResult(
            content=tuple(content),
            structured_content=structured_content,
            is_error=is_error,
            meta=meta,
        )

    async def _handle_inbound_message(self, payload: bytes | str) -> None:
        await self._session.receive(payload)

    def _handle_transport_close(self, error: Any) -> None:
        self._connected = False
        self._session.close(error)

    async def _on_tools_list_changed(self, _params: Any | None) -> None:
        if (
            not self.connected
            or not self._list_changed
            or self._tools_changed_handler is None
        ):
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(
            self._tools_changed_handler(),
            name=f"mcp-tools-refresh-{self.server_id}",
        )
        self._refresh_task.add_done_callback(self._finish_refresh_task)

    def _finish_refresh_task(self, task: asyncio.Task[None]) -> None:
        with suppress(BaseException):
            task.result()
