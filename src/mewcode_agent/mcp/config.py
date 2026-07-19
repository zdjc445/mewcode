"""Strict loading for the user-global ``mcp_servers.yaml`` file."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import re
from typing import Any, cast
from urllib.parse import urlsplit

import yaml

from mewcode_agent.mcp.models import (
    McpConfigError,
    McpConfiguration,
    McpServerConfig,
    StdioServerConfig,
    StreamableHttpServerConfig,
)
from mewcode_agent.security import PathSandbox, PathSandboxError
from mewcode_agent.tools.base import ToolCategory

_ROOT_KEYS = {"version", "servers"}
_DISABLED_KEYS = {"enabled"}
_STDIO_KEYS = {
    "enabled",
    "required",
    "transport",
    "command",
    "args",
    "cwd",
    "env",
    "connect_timeout_seconds",
    "request_timeout_seconds",
    "shutdown_timeout_seconds",
    "tool_categories",
}
_HTTP_KEYS = {
    "enabled",
    "required",
    "transport",
    "url",
    "header_env",
    "connect_timeout_seconds",
    "request_timeout_seconds",
    "shutdown_timeout_seconds",
    "tool_categories",
}
_SERVER_ID = re.compile(r"[a-z][a-z0-9_]{0,23}")
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_RESERVED_HEADERS = {
    "accept",
    "content-type",
    "mcp-protocol-version",
    "mcp-session-id",
    "last-event-id",
    "origin",
}
_TOOL_CATEGORIES = {"read", "write", "command"}
_YAML_BOOL_TAG = "tag:yaml.org,2002:bool"


class _StrictLoader(yaml.SafeLoader):
    """Safe YAML loader with exact booleans and duplicate-key rejection."""


_StrictLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern)
        for tag, pattern in resolvers
        if tag != _YAML_BOOL_TAG
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictLoader.add_implicit_resolver(
    _YAML_BOOL_TAG,
    re.compile(r"^(?:true|false)$"),
    ["t", "f"],
)


def _construct_mapping(
    loader: _StrictLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def default_mcp_config_path(*, home_directory: Path | None = None) -> Path:
    """Return the sole MCP configuration path used by the application."""

    home = Path.home() if home_directory is None else home_directory
    return home / ".mewcode-agent" / "mcp_servers.yaml"


def _expect_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise McpConfigError(f"{location} 必须是映射")
    if any(not isinstance(key, str) for key in value):
        raise McpConfigError(f"{location} 的字段名必须是字符串")
    return cast(Mapping[str, Any], value)


def _validate_exact_keys(
    data: Mapping[str, Any],
    expected: set[str],
    location: str,
) -> None:
    actual = set(data)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise McpConfigError(
            f"{location} 缺少字段: {', '.join(missing)}"
        )
    if extra:
        raise McpConfigError(
            f"{location} 包含未知字段: {', '.join(extra)}"
        )


def _parse_timeout(value: Any, location: str) -> float:
    if type(value) not in (int, float) or value <= 0:
        raise McpConfigError(f"{location} 必须是大于 0 的数字")
    return float(value)


def _parse_string_mapping(value: Any, location: str) -> Mapping[str, str]:
    data = _expect_mapping(value, location)
    parsed: dict[str, str] = {}
    for key, item in data.items():
        if not key:
            raise McpConfigError(f"{location} 的键必须是非空字符串")
        if not isinstance(item, str) or not item:
            raise McpConfigError(f"{location}.{key} 必须是非空字符串")
        parsed[key] = item
    return parsed


def _resolve_environment(
    value: Any,
    *,
    location: str,
    environ: Mapping[str, str],
) -> dict[str, str]:
    references = _parse_string_mapping(value, location)
    resolved: dict[str, str] = {}
    for destination, source in references.items():
        if source not in environ:
            raise McpConfigError(
                f"{location}.{destination} 引用的环境变量 {source} 不存在"
            )
        resolved[destination] = environ[source]
    return resolved


def _parse_tool_categories(
    value: Any,
    location: str,
) -> dict[str, ToolCategory]:
    data = _expect_mapping(value, location)
    categories: dict[str, ToolCategory] = {}
    for tool_name, category in data.items():
        if not 1 <= len(tool_name) <= 128:
            raise McpConfigError(
                f"{location} 的工具名长度必须为 1 到 128"
            )
        if category not in _TOOL_CATEGORIES:
            raise McpConfigError(
                f"{location}.{tool_name} 必须为 read、write 或 command"
            )
        categories[tool_name] = cast(ToolCategory, category)
    return categories


def _parse_common(
    server_id: str,
    data: Mapping[str, Any],
) -> tuple[bool, float, float, float, dict[str, ToolCategory]]:
    location = f"servers.{server_id}"
    required = data["required"]
    if type(required) is not bool:
        raise McpConfigError(f"{location}.required 必须是布尔值")
    return (
        required,
        _parse_timeout(
            data["connect_timeout_seconds"],
            f"{location}.connect_timeout_seconds",
        ),
        _parse_timeout(
            data["request_timeout_seconds"],
            f"{location}.request_timeout_seconds",
        ),
        _parse_timeout(
            data["shutdown_timeout_seconds"],
            f"{location}.shutdown_timeout_seconds",
        ),
        _parse_tool_categories(
            data["tool_categories"],
            f"{location}.tool_categories",
        ),
    )


def _parse_stdio(
    server_id: str,
    data: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
    sandbox: PathSandbox,
) -> StdioServerConfig:
    location = f"servers.{server_id}"
    _validate_exact_keys(data, _STDIO_KEYS, location)
    required, connect_timeout, request_timeout, shutdown_timeout, categories = (
        _parse_common(server_id, data)
    )

    command = data["command"]
    if not isinstance(command, str) or not command.strip():
        raise McpConfigError(f"{location}.command 必须是非空字符串")
    args = data["args"]
    if not isinstance(args, list) or any(
        not isinstance(item, str) for item in args
    ):
        raise McpConfigError(f"{location}.args 必须是字符串列表")
    cwd = data["cwd"]
    if not isinstance(cwd, str) or not cwd.strip():
        raise McpConfigError(f"{location}.cwd 必须是非空字符串")
    try:
        resolved_cwd = sandbox.resolve(cwd)
    except PathSandboxError as exc:
        raise McpConfigError(f"{location}.cwd 超出应用工作目录") from exc
    child_environment = _resolve_environment(
        data["env"],
        location=f"{location}.env",
        environ=environ,
    )
    return StdioServerConfig(
        server_id=server_id,
        required=required,
        command=command,
        args=tuple(args),
        cwd=resolved_cwd,
        env=child_environment,
        connect_timeout_seconds=connect_timeout,
        request_timeout_seconds=request_timeout,
        shutdown_timeout_seconds=shutdown_timeout,
        tool_categories=categories,
    )


def _validate_url(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise McpConfigError(f"{location} 必须是非空字符串")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise McpConfigError(f"{location} 不是有效的绝对 URL") from exc
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise McpConfigError(
            f"{location} 必须是无 userinfo、无 fragment 的绝对 HTTP URL"
        )
    if port is not None and not 1 <= port <= 65535:
        raise McpConfigError(f"{location} 的端口无效")
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise McpConfigError(f"{location} 的非 loopback 地址必须使用 https")
    return value


def _parse_headers(
    value: Any,
    *,
    location: str,
    environ: Mapping[str, str],
) -> dict[str, str]:
    references = _parse_string_mapping(value, location)
    headers: dict[str, str] = {}
    for header_name, source in references.items():
        if not _HEADER_NAME.fullmatch(header_name):
            raise McpConfigError(f"{location} 包含无效 HTTP header 名")
        if header_name.lower() in _RESERVED_HEADERS:
            raise McpConfigError(
                f"{location} 不得覆盖保留 header {header_name}"
            )
        if source not in environ:
            raise McpConfigError(
                f"{location}.{header_name} 引用的环境变量 {source} 不存在"
            )
        header_value = environ[source]
        if "\r" in header_value or "\n" in header_value:
            raise McpConfigError(
                f"{location}.{header_name} 引用的环境变量不能包含换行"
            )
        try:
            header_value.encode("ascii")
        except UnicodeError as exc:
            raise McpConfigError(
                f"{location}.{header_name} 引用的环境变量必须可编码为 ASCII"
            ) from exc
        headers[header_name] = header_value
    return headers


def _parse_http(
    server_id: str,
    data: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
) -> StreamableHttpServerConfig:
    location = f"servers.{server_id}"
    _validate_exact_keys(data, _HTTP_KEYS, location)
    required, connect_timeout, request_timeout, shutdown_timeout, categories = (
        _parse_common(server_id, data)
    )
    return StreamableHttpServerConfig(
        server_id=server_id,
        required=required,
        url=_validate_url(data["url"], f"{location}.url"),
        headers=_parse_headers(
            data["header_env"],
            location=f"{location}.header_env",
            environ=environ,
        ),
        connect_timeout_seconds=connect_timeout,
        request_timeout_seconds=request_timeout,
        shutdown_timeout_seconds=shutdown_timeout,
        tool_categories=categories,
    )


def _parse_server(
    server_id: str,
    value: Any,
    *,
    environ: Mapping[str, str],
    sandbox: PathSandbox,
) -> McpServerConfig | None:
    location = f"servers.{server_id}"
    data = _expect_mapping(value, location)
    enabled = data.get("enabled")
    if type(enabled) is not bool:
        raise McpConfigError(f"{location}.enabled 必须是布尔值")
    if not enabled:
        _validate_exact_keys(data, _DISABLED_KEYS, location)
        return None
    transport = data.get("transport")
    if transport == "stdio":
        return _parse_stdio(
            server_id,
            data,
            environ=environ,
            sandbox=sandbox,
        )
    if transport == "streamable_http":
        return _parse_http(server_id, data, environ=environ)
    raise McpConfigError(
        f"{location}.transport 必须为 stdio 或 streamable_http"
    )


def load_mcp_config(
    *,
    working_directory: Path,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> McpConfiguration:
    """Load enabled MCP servers from the sole user-global config schema."""

    config_path = default_mcp_config_path() if path is None else path
    if not config_path.exists():
        return McpConfiguration()
    if not config_path.is_file():
        raise McpConfigError(f"MCP 配置路径不是文件: {config_path}")
    try:
        raw = yaml.load(
            config_path.read_text(encoding="utf-8"),
            Loader=_StrictLoader,
        )
    except (OSError, UnicodeError) as exc:
        raise McpConfigError(f"无法读取 MCP 配置: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise McpConfigError(f"MCP 配置不是有效 YAML: {config_path}") from exc

    root = _expect_mapping(raw, "MCP 配置根节点")
    _validate_exact_keys(root, _ROOT_KEYS, "MCP 配置根节点")
    if type(root["version"]) is not int or root["version"] != 1:
        raise McpConfigError("MCP 配置 version 必须为整数 1")
    raw_servers = _expect_mapping(root["servers"], "MCP 配置 servers")
    environment = os.environ if environ is None else environ
    try:
        sandbox = PathSandbox(working_directory)
    except PathSandboxError as exc:
        raise McpConfigError("MCP 配置的应用工作目录无效") from exc

    servers: list[McpServerConfig] = []
    for server_id, value in raw_servers.items():
        if not _SERVER_ID.fullmatch(server_id):
            raise McpConfigError(
                "MCP server ID 必须匹配 [a-z][a-z0-9_]{0,23}"
            )
        server = _parse_server(
            server_id,
            value,
            environ=environment,
            sandbox=sandbox,
        )
        if server is not None:
            servers.append(server)
    return McpConfiguration(servers=tuple(servers))
