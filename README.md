# Mewcode Agent

一个使用 Textual 构建的终端 LLM Agent。当前版本通过 OpenAI 兼容协议或 Anthropic 兼容协议连接 DeepSeek，支持流式输出、可恢复的多轮对话、分层项目上下文和自动工具调用循环。

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

## 项目指令

应用启动时读取两层 Markdown 指令，并在第一条真实用户消息之前作为独立指令消息注入：

1. 项目级：`Path.cwd() / "MEWCODE.md"`
2. 用户级：`Path.home() / ".mewcode-agent" / "INSTRUCTIONS.md"`

项目级优先于用户级。入口文件不存在表示该层没有指令；文件存在但不是有效 UTF-8、不是普通文件或超过限制时，应用拒绝启动。

指令文件可用独占一行的 `@include relative/path.md` 在原位置展开其他文件。include 只接受所属配置根目录内的精确相对路径，拒绝绝对路径、`..` 越界、符号链接越界和循环引用；最大嵌套深度为 `5`，单文件上限为 `64 KiB`，单层展开结果上限为 `256 KiB`。

项目文件中的指令不能授予工具权限、扩大当前请求范围或覆盖代码层安全策略。

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

## Hook 自动化配置

声明式 Hook 在应用启动时从两层严格 YAML 加载：

1. 项目级：`Path.cwd() / ".mewcode" / "hooks.yaml"`
2. 用户级：`Path.home() / ".mewcode-agent" / "hooks.yaml"`

项目规则先执行，并完整覆盖精确同 `id` 的用户规则。文件不存在表示该层没有 Hook；重复键、未知字段、无效 matcher 或动作组合会定位到精确规则字段并阻止启动。修改配置后需要重启应用。

```yaml
version: 1
rules:
  - id: audit_writes
    event: tool.before_execute
    once: false
    async: true
    timeout_seconds: 10
    match:
      tool.name:
        kind: exact
        pattern: write_file
      file.path:
        kind: not
        pattern:
          kind: glob
          pattern: ".git/**"
    action:
      type: http
      method: POST
      url: "https://example.test/hooks"
      headers:
        Content-Type: application/json
      body: '{"event":"${event.name}","path":"${file.path}"}'
    intercept: null
```

事件名固定覆盖 `system.*`、`context.*`、`session.*`、`round.*`、`message.*` 和 `tool.*` 生命周期，完整清单与字段见 [`docs/ch10/spec.md`](docs/ch10/spec.md)。条件使用 `{kind, pattern}`，支持类型敏感的 `exact`、大小写敏感的 `glob`、完整值 `regex` 和最大八层递归 `not`；同一规则的所有字段必须同时命中。字段名和工具参数键都按原文精确匹配，不转换大小写或猜测别名。`${...}` 只读取该事件明确提供的上下文字段，未知字段使该次动作失败，不会替换为空字符串。

动作支持：

- `shell`：在项目根目录使用 PowerShell 或 `/bin/sh` 执行明确 command；stdout/stderr 不进入模型。
- `prompt`：作为 request 级 Prompt control 注入，不写入普通历史或会话 JSONL；无活动 request 时排队到下一次请求。
- `http`：使用共享异步客户端发送绝对 HTTP(S) URL，不跟随重定向，响应正文不进入模型。
- `subagent`：当前返回脱敏的 `hook_subagent_unavailable` 诊断，Chapter 11 接入统一子工作者后启用真实执行。

`async: true` 的 shell、HTTP 或 subagent 动作不会阻塞 Agent，退出时会在各自超时内等待收尾。`once: true` 在当前应用进程内最多调度一次，失败或超时不自动重试。同步 `tool.before_execute` 规则可以使用 `intercept: {deny: true, reason: ...}` 返回 `tool_blocked_by_hook`；它只会进一步拒绝已经通过安全策略与审批的调用，不能授予权限或修改工具参数/结果。

Hook 动作失败、超时或模板错误只输出不含 command、Prompt、HTTP 数据、工具参数和环境值的本地诊断，不改变 Agent 原始结果。Hook 配置不会自动创建、修改或清理。

项目级 Hook 能直接执行 shell 并向网络发送规则作者选择的模板数据。只应在明确受信任的工作区中放置或启用 `.mewcode/hooks.yaml`；工具安全规则不会对 Hook 自身授予操作再次弹出审批。

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

## 上下文压缩

每次普通模型请求前按固定顺序执行两层上下文处理：

1. 单个工具结果严格大于 `64 KiB`，或同一工具批次的内联合计严格大于 `128 KiB` 时，把完整紧凑 JSON 写入当前会话的 artifact 目录；历史只保留头尾正文预算为 `8 KiB` 的预览、绝对路径、SHA-256 和原始字节数。
2. Prompt 估值达到有效窗口的 `80%` 时，使用当前 Provider 和模型发起 `tools=None` 的结构化摘要请求；新 checkpoint 只有在估值下降并达到 `60%` 目标后才会提交。

用户原始消息不会交给摘要模型改写。压缩投影仍以原始 `user` 消息保留每条用户输入，并由代码在 checkpoint 中生成逐字符相同的校验副本。摘要后的边界消息要求模型在需要精确文件、代码、日志或工具结果时重新读取，不能根据摘要补全细节。

外置文件位于：

```text
Path.home() / ".mewcode-agent" / "context-artifacts" / <session_id> / "tool-results"
```

只有本次会话已登记的精确绝对路径可以通过 `read_context_artifact` 分页读取。应用正常退出时删除当前 session 目录；下次启动清理超过 `24` 小时且名称严格匹配会话 ID 格式的崩溃遗留目录。

在没有活动 Agent run 时输入 `/compact` 或 `/compress`，可手动压缩历史并默认保留最新 `4` 个原子历史单元。命令名大小写不敏感，但命令不接受参数。自动摘要连续失败 `3` 次后熔断；手动压缩成功会恢复自动压缩。

context artifact 是工具结果外置产生的临时文件。它的退出和陈旧目录清理不会删除下面的会话存档。

## 会话存档与恢复

普通 user、assistant 和 tool 消息会追加写入：

```text
Path.home() / ".mewcode-agent" / "sessions" / <session_id> / "messages.jsonl"
```

同目录的 `meta.json` 保存会话列表所需的 ID、项目路径、标题、摘要、消息数和时间等概要。空会话不创建目录；写入顺序是先持久化 JSONL 并同步，再更新内存历史和 meta。恢复时会跳过独立坏行，并把不完整的工具调用批次截断到最后一个完整边界后修复存档。

会话存档不会按时间、数量或磁盘空间自动清理，也不会在应用退出时删除。只有用户在确认界面确认精确的 `/session delete <session_id>` 后，才能删除非活动会话。

可用命令：

- `/sessions`：列出当前项目的会话，只读取 `meta.json`。
- `/resume <session_id>`：恢复并切换到当前项目的指定会话。
- `/session path <session_id>`：显示会话目录的精确绝对路径。
- `/session delete <session_id>`：确认后删除指定的非活动会话。

`session_id` 是 32 位小写十六进制字符串，不会转换大写或修复格式。命令名大小写不敏感，子命令和参数保持区分大小写。命令不会进入普通历史；参数无效时只在本地显示用法。距上次活跃达到 `7` 天的恢复会话会收到非授权型时间跨度提醒；恢复结果达到 Prompt 预算时先尝试一次现有上下文压缩。

## 分层自动笔记

用户级和项目级笔记分别保存到：

```text
Path.home() / ".mewcode-agent" / "notes.md"
Path.cwd() / ".mewcode" / "notes.md"
```

用户偏好和纠正反馈进入用户级笔记，项目知识和参考资料进入项目级笔记。非空笔记以辅助 context 注入，不是指令、授权或文件事实；需要精确状态时仍须重新读取来源。

每 `5` 个成功完成的用户请求触发一次异步更新；退出时若存在尚未处理的新对话，再等待一次更新，最长 `120` 秒。更新使用当前 Provider，明确设置 `tools=None`，并严格校验固定 JSON 结构；语义合并和去重由 LLM 完成，代码不实现相似度算法。

可用命令（`notes` 是规范命令 `memory` 的兼容别名）：

- `/memory`、`/notes`：显示四类当前笔记。
- `/memory paths`、`/notes paths`：显示两份笔记文件的精确绝对路径。
- `/memory clear user`、`/notes clear user`：确认后清空用户级笔记。
- `/memory clear project`、`/notes clear project`：确认后清空项目级笔记。

笔记命令不进入普通历史，清空必须经过确认。应用不会自动清空笔记。

## Skills

Skill 把严格元数据、Markdown SOP 和可选的 Python 工具组织成按需加载的能力包。来源优先级固定为：

1. 项目级：`Path.cwd() / ".mewcode" / "skills"`
2. 用户级：`Path.home() / ".mewcode-agent" / "skills"`
3. 内置：Python 包中的 `mewcode_agent/builtin_skills`

同名 Skill 由高优先级来源完整覆盖。支持直接子文件 `<name>.md` 和目录 `<name>/SKILL.md`；单个无效候选会被跳过并输出脱敏诊断，高优先级候选无效时允许低优先级同名候选生效。最终生效 Skill 引用不存在的工具、专属工具重名或命令名冲突时，应用拒绝启动。

Skill 文档必须使用以下精确 frontmatter：

```markdown
---
name: example
description: 一句话说明
allowed_tools:
  - read_file
execution_mode: shared
model: inherit
context_strategy: current
recent_messages: null
---
# SOP

这里是加载后才发送给模型的完整操作步骤。
```

`name` 必须匹配 `[a-z][a-z0-9-]*`；字段不能缺失，也不能增加未知字段。`shared` 只能使用 `current`；`isolated` 可以使用 `summary`、`recent` 或 `none`，其中 `recent` 必须提供正整数 `recent_messages`。当前版本的 `model` 只接受 `inherit`。

启动 Prompt 只公开 Skill 的 `name` 和 `description`。Agent 需要使用 Skill 时调用系统工具 `load_skill`，完整 SOP 才会固定到当前环境上下文；它不写入普通消息历史或会话 JSONL。多个 shared Skill 同时激活时，可见工具是各自 `allowed_tools` 的交集，并始终保留 `load_skill`。工具可见性不会绕过路径沙箱、安全规则或审批。

isolated Skill 使用独立历史和运行时，只把最终响应作为工具结果回流。`summary` 通过禁止工具的现有摘要器携带完整历史摘要，`recent` 携带最近 N 条并保持工具事务完整，`none` 不携带主历史。隔离执行遇到新的工具确认请求时固定拒绝该次调用；已经被安全策略、会话规则或永久规则允许的调用正常执行。

目录 Skill 可以额外提供严格的 `tools.yaml`：

```yaml
version: 1
tools:
  - name: example_tool
    description: 一句话说明
    parameters:
      type: object
      properties: {}
      additionalProperties: false
    category: command
    timeout_seconds: 30
    script: tools/example_tool.py
```

专属工具名必须匹配 `[a-z][a-z0-9_]{0,63}`。脚本以当前 Python 解释器作为无 shell 子进程运行，cwd 固定为 Skill 目录，从 stdin 读取一个 UTF-8 JSON object，并向 stdout 写出一个完整 JSON value。扫描、帮助和激活不会执行脚本；实际调用必须先通过参数 schema、当前 Skill 白名单和现有 `command` 权限审批。stderr、traceback 和脚本源码不会回传模型。

内置样板包括 shared `commit`、isolated recent `review` 和 isolated summary `test`。每个生效 Skill 自动注册 `/<name> [arguments]`；执行前会重新读取源文件以应用 SOP 和工具热更新。新增、删除、重命名或覆盖关系变化需要执行 `/skills rescan`。

## 斜杠命令

所有 `/` 前缀输入先进入集中式命令注册中心。命令名是第一个 ASCII 空格前的部分，按小写解析，因此 `/HELP`、`/Help` 和 `/help` 等价；参数原文不转换大小写、路径或标识符。未知命令不会发送给模型，统一引导使用 `/help`。

| 命令 | 别名 | 行为 |
| --- | --- | --- |
| `/help [command]` | `/h`、`/?` | 显示命令总览或单条命令的元数据和用法 |
| `/status` | `/stat` | 显示模型、模式、会话、Prompt Token 估值、笔记和权限状态 |
| `/mode [plan\|execute]` | 无 | 查看或切换后续普通消息的默认模式 |
| `/skills [show <name>\|rescan]` | 无 | 查看 Skill、显示脱敏详情或原子重新扫描 |
| `/commit [arguments]` | 无 | 加载内置或被覆盖的 shared 提交 Skill |
| `/review [arguments]` | 无 | 加载内置或被覆盖的 isolated 代码审查 Skill |
| `/test [arguments]` | 无 | 加载内置或被覆盖的 isolated 测试 Skill |
| `/compact` | `/compress` | 手动执行上下文压缩 |
| `/clear` | `/new` | 保留旧会话存档并切换到新的 lazy 空会话 |
| `/sessions` | 无 | 列出当前项目的已保存会话 |
| `/resume <session_id>` | 无 | 恢复指定会话 |
| `/session <path\|delete> <session_id>` | 无 | 定位或确认删除非活动会话 |
| `/memory [...]` | `/notes` | 查看、定位或确认清空分层笔记 |
| `/permissions [...]` | `/perms` | 查看权限状态或设置当前进程模式覆盖 |

`/permissions strict`、`/permissions default` 和 `/permissions permissive` 只覆盖当前应用进程中未命中规则的默认处理，`/permissions reset` 恢复启动配置。它不会写入安全 YAML 或永久审批文件，也不能绕过内置危险操作拒绝、路径沙箱或显式安全规则。

所有动态 Skill 命令固定以 execute 方式发送统一合成请求，但不会改变状态栏中的默认模式。Chapter 08 的 `/code-review` 兼容别名已经移除；自定义 Skill 也会按精确 `name` 出现在帮助和补全中。

输入框中只有一个公开命令前缀匹配时，Tab 会直接补全；多个匹配时弹出可用 Up、Down、Enter 和 Escape 操作的候选列表。隐藏命令不参与帮助、状态栏提示或补全。状态栏持续显示 `mode=plan|execute` 和 `/help /status /compact`。

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
- 支持 JSONL 持久会话、meta 列表、安全恢复和显式删除；不自动清理会话存档。
- 支持项目级优先的两层 Markdown 指令和受限 `@include`。
- 支持用户级与项目级分流的自动笔记、查看、定位和确认清空。
- 支持集中式斜杠命令注册、别名冲突检查、大小写不敏感分发、帮助和 Tab 补全。
- 支持项目、用户、内置三层 Skill，按需加载 SOP、最小工具白名单、shared/isolated 执行和动态命令。
- 支持目录 Skill 的严格 JSON Schema 与无 shell Python 子进程工具协议。
- 支持两层声明式 Hook、生命周期事件、同步/异步动作、Prompt 注入、HTTP、shell 和工具拒绝拦截。
- 支持本地状态和进程内权限模式覆盖。
- 内置 `read_file`、`write_file`、`edit_file`、`run_command`、`find_files`、`search_code` 和会话限定的 `read_context_artifact`。
- 支持通过 stdio 或 Streamable HTTP 发现并复用 MCP 远端工具；当前只实现 MCP Tools，不实现 Resources、Prompts、OAuth 或 Tasks。
- 每次用户请求最多执行 `15` 个模型轮；工具结果会立即写入对话历史并回灌模型，直到模型返回最终文本。
- 模型在同一次响应中返回多个工具调用时，按响应索引顺序逐个执行。
- 第 `15` 个模型轮不提供工具定义，并要求模型根据已有结果生成最终答复。
- 文件工具支持项目内的相对路径和绝对路径；规范化结果超出启动工作目录时拒绝执行。
- 工具失败以结构化结果写入历史，不会导致应用退出。
- 支持 `/compact`、`/compress` 手动命令和自动两级上下文压缩。
- 上下文 artifact 只在当前进程会话内使用并按临时文件策略清理；会话 JSONL 和笔记不受该清理影响。
- 不实现向量检索、会话自动清理或本地相似度笔记去重。
