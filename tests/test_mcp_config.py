from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.mcp import (
    McpConfigError,
    StdioServerConfig,
    StreamableHttpServerConfig,
    default_mcp_config_path,
    load_mcp_config,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _stdio_config(*, extra: str = "", env: str = "PATH: PARENT_PATH") -> str:
    return f"""version: 1
servers:
  local_server:
    enabled: true
    required: true
    transport: stdio
    command: python
    args: [\"-m\", \"fake_server\"]
    cwd: .
    env:
      {env}
    connect_timeout_seconds: 10
    request_timeout_seconds: 30
    shutdown_timeout_seconds: 5
    tool_categories:
      Read.File: read
{extra}"""


def _http_config(
    *,
    url: str = "https://example.com/mcp",
    header: str = "Authorization: MCP_AUTH",
) -> str:
    return f"""version: 1
servers:
  remote_server:
    enabled: true
    required: false
    transport: streamable_http
    url: {url}
    header_env:
      {header}
    connect_timeout_seconds: 10
    request_timeout_seconds: 60
    shutdown_timeout_seconds: 5
    tool_categories: {{}}
"""


def test_default_path_is_only_user_global_path(tmp_path: Path) -> None:
    assert default_mcp_config_path(home_directory=tmp_path) == (
        tmp_path / ".mewcode-agent" / "mcp_servers.yaml"
    )


def test_missing_file_returns_empty_configuration(tmp_path: Path) -> None:
    config = load_mcp_config(
        working_directory=tmp_path,
        path=tmp_path / "missing.yaml",
        environ={},
    )

    assert config.servers == ()


def test_valid_stdio_config_resolves_cwd_and_explicit_environment(
    tmp_path: Path,
) -> None:
    secret = "stdio-secret-value"
    path = _write(tmp_path / "mcp.yaml", _stdio_config())

    config = load_mcp_config(
        working_directory=tmp_path,
        path=path,
        environ={"PARENT_PATH": secret, "UNDECLARED": "not-inherited"},
    )

    assert len(config.servers) == 1
    server = config.servers[0]
    assert isinstance(server, StdioServerConfig)
    assert server.server_id == "local_server"
    assert server.transport == "stdio"
    assert server.required is True
    assert server.command == "python"
    assert server.args == ("-m", "fake_server")
    assert server.cwd == tmp_path.resolve()
    assert dict(server.env) == {"PATH": secret}
    assert "UNDECLARED" not in server.env
    assert server.tool_categories == {"Read.File": "read"}
    assert secret not in repr(config)
    with pytest.raises(TypeError):
        server.env["NEW"] = "value"  # type: ignore[index]


def test_valid_http_config_resolves_headers_without_exposing_secret(
    tmp_path: Path,
) -> None:
    secret = "Bearer test-secret"
    path = _write(tmp_path / "mcp.yaml", _http_config())

    config = load_mcp_config(
        working_directory=tmp_path,
        path=path,
        environ={"MCP_AUTH": secret},
    )

    server = config.servers[0]
    assert isinstance(server, StreamableHttpServerConfig)
    assert server.transport == "streamable_http"
    assert server.url == "https://example.com/mcp"
    assert dict(server.headers) == {"Authorization": secret}
    assert secret not in repr(config)


def test_duplicate_yaml_key_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mcp.yaml",
        "version: 1\nversion: 1\nservers: {}\n",
    )

    with pytest.raises(McpConfigError, match="不是有效 YAML"):
        load_mcp_config(working_directory=tmp_path, path=path, environ={})


@pytest.mark.parametrize(
    "root",
    [
        "version: true\nservers: {}\n",
        "version: 2\nservers: {}\n",
        "version: 1\nservers: {}\nunknown: true\n",
        "version: 1\nservers: []\n",
    ],
)
def test_invalid_root_schema_is_rejected(tmp_path: Path, root: str) -> None:
    path = _write(tmp_path / "mcp.yaml", root)

    with pytest.raises(McpConfigError):
        load_mcp_config(working_directory=tmp_path, path=path, environ={})


@pytest.mark.parametrize(
    "server_id",
    ["Upper", "with-hyphen", "1server", "a" * 25],
)
def test_invalid_server_id_is_rejected(
    tmp_path: Path,
    server_id: str,
) -> None:
    path = _write(
        tmp_path / "mcp.yaml",
        f"version: 1\nservers:\n  {server_id}:\n    enabled: false\n",
    )

    with pytest.raises(McpConfigError, match="server ID"):
        load_mcp_config(working_directory=tmp_path, path=path, environ={})


def test_disabled_server_does_not_resolve_environment(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mcp.yaml",
        "version: 1\nservers:\n  off:\n    enabled: false\n",
    )

    config = load_mcp_config(
        working_directory=tmp_path,
        path=path,
        environ={},
    )

    assert config.servers == ()


def test_disabled_server_rejects_other_fields(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mcp.yaml",
        "version: 1\nservers:\n  off:\n    enabled: false\n"
        "    env:\n      TOKEN: MISSING_SECRET\n",
    )

    with pytest.raises(McpConfigError, match="包含未知字段: env"):
        load_mcp_config(working_directory=tmp_path, path=path, environ={})


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("required: true", "required: yes-value", "required"),
        ("command: python", "command: ''", "command"),
        ('args: [\"-m\", \"fake_server\"]', "args: [-m, 1]", "args"),
        ("connect_timeout_seconds: 10", "connect_timeout_seconds: true", "connect_timeout_seconds"),
        ("request_timeout_seconds: 30", "request_timeout_seconds: 0", "request_timeout_seconds"),
        ("Read.File: read", "Read.File: unsafe", "Read.File"),
    ],
)
def test_invalid_stdio_field_is_rejected(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    path = _write(tmp_path / "mcp.yaml", _stdio_config().replace(old, new))

    with pytest.raises(McpConfigError, match=message):
        load_mcp_config(
            working_directory=tmp_path,
            path=path,
            environ={"PARENT_PATH": "safe"},
        )


def test_stdio_cwd_must_stay_inside_working_directory(tmp_path: Path) -> None:
    working = tmp_path / "workspace"
    working.mkdir()
    path = _write(
        tmp_path / "mcp.yaml",
        _stdio_config().replace("cwd: .", "cwd: .."),
    )

    with pytest.raises(McpConfigError, match="cwd 超出"):
        load_mcp_config(
            working_directory=working,
            path=path,
            environ={"PARENT_PATH": "safe"},
        )


def test_missing_environment_reference_is_safe(tmp_path: Path) -> None:
    secret = "must-not-appear"
    path = _write(tmp_path / "mcp.yaml", _stdio_config())

    with pytest.raises(McpConfigError) as caught:
        load_mcp_config(
            working_directory=tmp_path,
            path=path,
            environ={"OTHER": secret},
        )

    assert caught.value.code == "mcp_config_error"
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "ftp://example.com/mcp",
        "https://user:password@example.com/mcp",
        "https://example.com/mcp#fragment",
        "https://example.com:99999/mcp",
        "/relative/mcp",
    ],
)
def test_invalid_http_url_is_rejected(tmp_path: Path, url: str) -> None:
    path = _write(tmp_path / "mcp.yaml", _http_config(url=f'"{url}"'))

    with pytest.raises(McpConfigError, match="url"):
        load_mcp_config(
            working_directory=tmp_path,
            path=path,
            environ={"MCP_AUTH": "secret"},
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/mcp",
        "http://127.0.0.1:8080/mcp",
        "http://[::1]/mcp",
    ],
)
def test_http_loopback_urls_are_allowed(tmp_path: Path, url: str) -> None:
    path = _write(tmp_path / "mcp.yaml", _http_config(url=f'"{url}"'))

    config = load_mcp_config(
        working_directory=tmp_path,
        path=path,
        environ={"MCP_AUTH": "secret"},
    )

    assert config.servers[0].url == url  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "header",
    [
        "accept: MCP_AUTH",
        "Content-Type: MCP_AUTH",
        "MCP-Protocol-Version: MCP_AUTH",
        "MCP-Session-Id: MCP_AUTH",
        "Last-Event-ID: MCP_AUTH",
        "Origin: MCP_AUTH",
    ],
)
def test_reserved_http_headers_are_rejected(
    tmp_path: Path,
    header: str,
) -> None:
    path = _write(tmp_path / "mcp.yaml", _http_config(header=header))

    with pytest.raises(McpConfigError, match="保留 header"):
        load_mcp_config(
            working_directory=tmp_path,
            path=path,
            environ={"MCP_AUTH": "secret"},
        )


def test_unknown_enabled_server_field_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mcp.yaml",
        _stdio_config(extra="    unknown: true\n"),
    )

    with pytest.raises(McpConfigError, match="包含未知字段: unknown"):
        load_mcp_config(
            working_directory=tmp_path,
            path=path,
            environ={"PARENT_PATH": "safe"},
        )
