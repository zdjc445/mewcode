# Mewcode Agent

一个使用 Textual 构建的终端 LLM Agent。当前版本通过 OpenAI 兼容协议或 Anthropic 兼容协议连接 DeepSeek，支持流式输出、进程内多轮对话和自动工具调用循环。

## 环境要求

- Python `3.11.9`
- `uv`
- 有效的 DeepSeek API Key

## 安装

```powershell
uv sync
```

## 配置 API Key

API Key 只通过环境变量提供，不写入 `llm_providers.yaml`：

```powershell
$env:DEEPSEEK_API_KEY = "你的 DeepSeek API Key"
```

环境变量只对当前 PowerShell 进程及其子进程生效。

## Prompt 配置

Prompt 配置按以下顺序在应用启动时加载一次：

1. 用户全局：`Path.home() / ".mewcode-agent" / "prompts.yaml"`
2. 当前项目：`Path.cwd() / ".mewcode" / "prompts.yaml"`

项目层对精确同名 `id` 的设置优先。修改配置后需要重启应用。

```yaml
version: 1
modules:
  - id: coding.project_rules
    enabled: true
    priority: 500
    content: |-
      修改代码前先读取相关文件。
      完成修改后运行与改动直接相关的验证。

  - id: output.default_style
    enabled: false
```

模块 `id` 必须完整匹配 `[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*`，不会自动转换大小写或字符。`enabled: false` 只能精确禁用此前已经存在的可配置模块。`core` 和 `core.` 命名空间受保护，用户全局和项目配置都不能声明、覆盖或禁用其中的模块。配置文件不存在属于正常状态；文件存在但结构无效时应用拒绝启动。

## 工具安全配置

工具安全配置在应用启动时加载：

1. 用户全局：`Path.home() / ".mewcode-agent" / "security.yaml"`
2. 当前项目：`Path.cwd() / ".mewcode" / "security.yaml"`
3. UI 生成的永久审批：`Path.home() / ".mewcode-agent" / "security-approvals.yaml"`

用户全局配置可以设置 `mode`；项目配置只能声明 `version` 和 `rules`，不能降低用户选择的权限模式。以下是用户全局配置示例：

```yaml
version: 1
mode: default
rules:
  - id: command.allow_tests
    action: allow
    tool: run_command
    priority: 100
    match:
      command:
        kind: glob
        pattern: "uv run pytest*"

  - id: write.confirm_config
    action: ask
    tool: write_file
    priority: 200
    match:
      path:
        kind: path_glob
        pattern: "*.yaml"
```

每条规则必须精确包含 `id`、`action`、`tool`、`priority` 和 `match`。`action` 只能是 `allow`、`deny` 或 `ask`；matcher 的 `kind` 只能是 `exact`、`glob` 或 `path_glob`。同一规则中的 matcher 全部命中时规则才命中。`path_glob` 只匹配内置工具的 `path` 或 `cwd` 路径参数。

规则分层顺序为：会话临时规则、项目规则、用户全局规则。相同层中先比较较大的 `priority`；相同优先级按 `deny`、`ask`、`allow` 排序，最后按规则 `id` 排序。内置危险命令拒绝和路径沙箱始终先于配置规则，不能被规则、计划批准或权限模式覆盖。

权限模式只处理没有命中规则的调用：

| mode | read | write | command |
| --- | --- | --- | --- |
| `strict` | 询问 | 询问 | 询问 |
| `default` | 允许 | 询问 | 询问 |
| `permissive` | 允许 | 允许 | 允许 |

审批界面支持“仅允许这一次”“本会话允许”“永久允许”和“拒绝”。永久审批只保存工具调用安全指纹和项目根目录，不保存命令正文、文件内容或编辑内容；项目规则仍然高于永久审批。

内置文件工具只能访问应用启动工作目录以内的规范化路径，并拒绝 `..`、绝对路径越界和符号链接越界。`run_command` 的 `cwd` 同样必须在工作目录内，并会拒绝已知破坏性命令和远程下载后直接执行的命令。`run_command` 仍然使用 PowerShell 或 `/bin/sh` 执行原始命令字符串，本项目当前没有提供操作系统级进程沙箱，因此危险命令检查不能等同于完整的文件系统、网络或进程隔离。

## MCP 工具配置

应用支持 MCP `2025-11-25` 的 Tools 能力，以及本地子进程 `stdio` 和远程 `streamable_http` 两种传输。MCP 配置只从用户全局路径加载：

```text
Path.home() / ".mewcode-agent" / "mcp_servers.yaml"
```

项目中的 `.mewcode/mcp_servers.yaml` 不会被读取，避免打开项目时自动启动进程或发起网络请求。配置文件不存在表示不启用外部 MCP server。

```yaml
version: 1
servers:
  local_example:
    enabled: true
    required: true
    transport: stdio
    command: python
    args: ["-m", "example_mcp_server"]
    cwd: "."
    env:
      PATH: PATH
      SYSTEMROOT: SYSTEMROOT
      EXAMPLE_TOKEN: EXAMPLE_MCP_TOKEN
    connect_timeout_seconds: 10
    request_timeout_seconds: 30
    shutdown_timeout_seconds: 5
    tool_categories:
      get_status: read

  remote_example:
    enabled: true
    required: false
    transport: streamable_http
    url: "https://example.com/mcp"
    header_env:
      Authorization: EXAMPLE_MCP_AUTHORIZATION
    connect_timeout_seconds: 10
    request_timeout_seconds: 60
    shutdown_timeout_seconds: 5
    tool_categories: {}
```

`env` 的键是子进程环境变量名，值是父进程环境变量名；子进程不会隐式继承其他环境变量。`header_env` 同样只引用父进程环境变量，环境变量值需要自行包含完整的 `Bearer ` 等前缀。非 loopback HTTP server 必须使用 HTTPS；明文 HTTP 只允许 `localhost`、`127.0.0.1` 或 `::1`。

`required: true` 的 server 激活失败会阻止应用启动；optional server 失败时会输出不含 secret 的警告并跳过。远端工具默认安全类别为 `command`，因此在 `default` 安全模式下需要审批；只有在 `tool_categories` 中用精确、区分大小写的远端工具名声明后，才会改为 `read` 或 `write`。MCP annotations 不会改变安全分类。

## 启动

必须从项目根目录执行：

```powershell
uv run mewcode-agent
```

默认使用 `llm_providers.yaml` 中的 `deepseek_openai`。如需验证 Anthropic 兼容协议，将 `default_provider` 改为 `deepseek_anthropic` 后重启应用。

## 测试

默认测试不访问外网，也不需要 API Key：

```powershell
uv run pytest
```

真实 API 集成测试需要先设置 `DEEPSEEK_API_KEY`：

```powershell
uv run pytest integration_tests
```

编译检查：

```powershell
uv run python -m compileall -q src tests integration_tests
```

## 当前范围

- 支持流式响应。
- 支持当前进程内的多轮对话。
- 内置 `read_file`、`write_file`、`edit_file`、`run_command`、`find_files` 和 `search_code` 六个工具。
- 支持通过 stdio 或 Streamable HTTP 发现并复用 MCP 远端工具；当前只实现 MCP Tools，不实现 Resources、Prompts、OAuth 或 Tasks。
- 每次用户请求最多执行 10 个工具；工具结果会立即写入对话历史并回灌模型，直到模型返回最终文本。
- 模型在同一次响应中返回多个工具调用时，按响应索引顺序逐个执行。
- 达到 10 次工具调用上限后，应用会关闭工具并要求模型根据已有结果生成最终总结。
- 文件工具支持项目内的相对路径和绝对路径；规范化结果超出启动工作目录时拒绝执行。
- 工具失败以结构化结果写入历史，不会导致应用退出。
- 不保存会话文件。
- 不包含斜杠命令或上下文压缩。
