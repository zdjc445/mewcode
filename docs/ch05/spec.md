# Chapter 05 Specification：MCP 工具客户端与双传输接入

## 1. 文档状态

- 状态：已实现，并通过 Chapter 05 自动化验收。
- 前置实现：Chapter 02 ReAct 工具循环、Chapter 03 模块化 Prompt、Chapter 04 工具安全策略与审批。
- 本章目标：实现一个基于 MCP `2025-11-25` 的工具客户端，通过 stdio 或 Streamable HTTP 连接外部 MCP server，发现其工具并包装成现有 `Tool`，使 Agent 可以通过现有注册、调度、安全审批和历史回填链路调用远端工具。

规范依据：

- [MCP 版本说明](https://modelcontextprotocol.io/docs/learn/versioning)
- [MCP 2025-11-25 生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)
- [MCP 2025-11-25 基础协议](https://modelcontextprotocol.io/specification/2025-11-25/basic/index)
- [MCP 2025-11-25 传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [MCP 2025-11-25 工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

## 2. 本实现采用的技术决策

| 项目 | 决策 |
| --- | --- |
| MCP 协议版本 | 只支持当前稳定版 `2025-11-25` |
| 旧版本兼容 | 不支持 `2024-*`、`2025-03-26`、`2025-06-18`，不回退到旧 HTTP+SSE |
| 非稳定协议版本 | 不支持 `2026-07-28-RC` 或 `draft` |
| MCP 功能范围 | 只消费 server 的 Tools 能力 |
| 标准传输 | stdio 与 Streamable HTTP |
| 协议实现 | 在项目内实现受限的 JSON-RPC/MCP 客户端，不依赖 MCP SDK 隐式管理生命周期 |
| Client capabilities | 初始化时发送空对象 `{}`，不声明未实现能力 |
| 工具发现 | 初始化完成后遍历 `tools/list` 全部分页 |
| 工具调用 | 使用普通 `tools/call`；不使用实验性 task augmentation |
| 连接复用 | 每个启用的 server 在应用会话内最多持有一个活动 MCP client |
| 配置位置 | 只读取用户全局 `Path.home() / ".mewcode-agent" / "mcp_servers.yaml"` |
| 项目配置 | 本章不读取 `.mewcode/mcp_servers.yaml`，避免项目文件在启动时触发外部进程或网络连接 |
| Secret 来源 | 只从父进程环境变量读取，不允许在 YAML 中写 secret 值 |
| 远端工具安全分类 | 默认一律为 `command`；MCP annotations 不自动降低权限 |
| 远端工具名称 | 使用稳定本地别名注册，远端原名保持精确、区分大小写且不做格式转换 |
| Server instructions | 作为不可信元数据保存用于诊断，不注入 System Prompt、控制消息或用户历史 |
| 启动策略 | required server 失败则启动失败；optional server 失败则跳过并输出脱敏警告 |
| 配置热更新 | 不支持；修改后重启应用 |

`2024-11-05-final` 是发布标签，不是线上协议值。本章发送的 `protocolVersion` 必须精确为 `2025-11-25`。

## 3. 项目范围

### 3.1 范围内

1. 严格解析 MCP server 配置。
2. 实现 JSON-RPC 2.0 request、response、error 和 notification 的校验与路由。
3. 使用唯一 request ID 异步关联乱序响应。
4. 实现 stdio 子进程传输。
5. 实现 Streamable HTTP 的 JSON 响应、POST SSE、GET SSE、session 和重连游标。
6. 实现 `initialize`、`notifications/initialized` 和能力协商。
7. 分页执行 `tools/list` 并校验工具定义。
8. 处理 `notifications/tools/list_changed` 并原子刷新该 server 的工具快照。
9. 将远端工具包装成现有 `Tool` 接口并注册到 `ToolRegistry`。
10. 使用普通 `tools/call` 调用远端工具并映射为现有 `ToolResult`。
11. 复用 Chapter 04 的安全策略、审批、超时和计划授权链路。
12. 应用退出时关闭 HTTP session、stdio 子进程、后台 reader 和全部 pending request。
13. 使用本地 fake server 覆盖协议、并发、错误和清理场景。

### 3.2 范围外

1. MCP Resources、Prompts、Roots、Sampling、Elicitation、Logging level 控制和 Completion。
2. 实验性 Tasks，包括 task-augmented `tools/call`、`tasks/get`、`tasks/result`、`tasks/list` 和 `tasks/cancel`。
3. OAuth 2.1、授权服务器发现、动态客户端注册、浏览器登录和 token refresh。
4. 旧 HTTP+SSE transport、WebSocket 或自定义 transport。
5. MCP Registry 搜索、server 安装、包下载或自动更新。
6. 项目级 MCP server 配置和配置热更新。
7. 将 server instructions、tool annotations 或远端内容提升为受信任 Prompt。
8. 跨进程连接池、连接磁盘缓存或工具列表磁盘缓存。
9. 自动重试可能已执行的 `tools/call`。
10. 在 TUI 中渲染 MCP icon、图片、音频或嵌入资源的专用组件。

## 4. 配置契约

### 4.1 文件位置与合规边界

唯一配置文件精确为：

```text
Path.home() / ".mewcode-agent" / "mcp_servers.yaml"
```

文件不存在表示没有外部 MCP server，应用只注册内置工具。文件存在时必须是 UTF-8 严格 YAML；重复键、未知字段、错误类型和重复 server ID 都使应用启动失败。

本章不读取项目目录中的 MCP 配置。原因是 stdio 配置可启动任意进程，Streamable HTTP 配置可向外部地址发起连接；仅打开一个未受信任项目不能自动获得这两项权限。

### 4.2 根结构

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

`version` 必须是整数 `1`，不能是布尔值。`servers` 必须是映射，可以为空。

### 4.3 Server ID

Server ID 必须完整匹配：

```text
[a-z][a-z0-9_]{0,23}
```

ID 区分大小写且不做转换；不满足该正则的 ID 直接拒绝。长度上限用于保证生成的本地工具别名始终落在现有 Provider 可接受的短名称范围内。

### 4.4 禁用项

禁用项只能精确包含：

```yaml
enabled: false
```

禁用项不解析环境变量、不创建 transport、不进入连接缓存，也不允许包含其他字段。

### 4.5 stdio 项

启用的 stdio 项必须精确包含：

```text
enabled
required
transport
command
args
cwd
env
connect_timeout_seconds
request_timeout_seconds
shutdown_timeout_seconds
tool_categories
```

约束如下：

- `enabled` 和 `required` 必须是布尔值；
- `transport` 必须精确为 `stdio`；
- `command` 必须是非空字符串；
- `args` 必须是字符串列表；
- `cwd` 必须是非空字符串，经 Chapter 04 `PathSandbox` 解析后必须位于应用启动工作目录内；
- `env` 必须是“子进程变量名 → 父进程变量名”的字符串映射；
- 环境变量名必须保持精确，不做大小写转换；
- 所有被引用的父进程环境变量都必须存在；
- 子进程环境精确由 `env` 映射生成，不隐式继承其他父进程变量；
- transport 使用 `create_subprocess_exec(command, *args)`，绝不把配置拼接成 shell 字符串；
- 三个 timeout 必须是大于 `0` 的整数或浮点数，不能是布尔值。

如果 server 需要 `PATH`、`SYSTEMROOT`、`HOME`、`USERPROFILE` 或其他操作系统变量，必须在 `env` 中逐项声明。缺少 server 启动所需变量属于 `mcp_connect_failed`，实现不得为兼容性静默扩大继承范围。

### 4.6 Streamable HTTP 项

启用的 Streamable HTTP 项必须精确包含：

```text
enabled
required
transport
url
header_env
connect_timeout_seconds
request_timeout_seconds
shutdown_timeout_seconds
tool_categories
```

约束如下：

- `transport` 必须精确为 `streamable_http`；
- `url` 必须是无 userinfo、无 fragment 的绝对 URL；
- 非 loopback 地址必须使用 `https`；
- `http` 只允许主机精确解析为 `localhost`、`127.0.0.1` 或 `::1`；
- 不自动跟随 HTTP redirect；
- `header_env` 是“HTTP header 名 → 父进程环境变量名”的字符串映射；
- 环境变量的完整值作为 header value，例如 `Authorization` 对应的环境变量需要自行包含 `Bearer ` 前缀；
- header 环境变量值必须能按 HTTP client 的精确要求编码为 ASCII，且不得包含 CR 或 LF；
- 配置不得覆盖 `Accept`、`Content-Type`、`MCP-Protocol-Version`、`MCP-Session-Id`、`Last-Event-ID` 或 `Origin`；比较按 HTTP header 的大小写不敏感语义执行；
- header 值、session ID、URL userinfo 和环境变量值不得进入日志、异常正文、`repr` 或模型历史。

`header_env` 只支持预先准备好的静态请求头，不等同于实现 MCP OAuth。

### 4.7 `tool_categories`

`tool_categories` 是“远端工具原名 → 本地安全分类”的映射。键必须与 `tools/list` 返回的 `name` 精确、区分大小写地相同；值只能是 `read`、`write` 或 `command`。

- 未配置的远端工具使用 `command`；
- 配置中存在但发现结果中不存在的工具名使该 server 激活失败；
- 不读取 `readOnlyHint`、`destructiveHint` 或其他 annotations 来自动分类；
- annotations 只作为不可信诊断元数据保存。

## 5. 总体架构

新增包：

```text
src/mewcode_agent/mcp/
├── __init__.py
├── models.py
├── config.py
├── protocol.py
├── client.py
├── manager.py
├── adapter.py
└── transports/
    ├── __init__.py
    ├── base.py
    ├── stdio.py
    └── streamable_http.py
```

| 模块 | 职责 |
| --- | --- |
| `mcp.models` | 不可变配置、server 信息、远端工具定义、连接状态和安全诊断类型 |
| `mcp.config` | 严格读取 `mcp_servers.yaml`，解析环境变量引用和 transport 联合结构 |
| `mcp.protocol` | JSON-RPC 2.0 消息校验、ID 分配、pending request 路由和标准错误 |
| `mcp.transports.base` | 传输统一接口和入站消息回调契约 |
| `mcp.transports.stdio` | 子进程、换行帧、stdout reader、stderr drain 和进程关闭 |
| `mcp.transports.streamable_http` | POST/GET、JSON/SSE、session header、版本 header 和 SSE 恢复 |
| `mcp.client` | MCP initialize、能力检查、工具分页发现、调用、取消和关闭 |
| `mcp.adapter` | 把一个精确远端工具包装为现有 `Tool` |
| `mcp.manager` | 多 server 激活、缓存、重连、工具快照替换和统一关闭 |

新增的 HTTP 与 JSON Schema 库必须作为直接项目依赖声明，不能依赖 OpenAI 或 Anthropic SDK 的传递依赖。

## 6. 应用生命周期与所有权

### 6.1 异步启动

当前同步 CLI 入口改为使用一个顶层 `asyncio.run()` 承载完整应用会话；Textual 使用同一事件循环中的异步运行入口。MCP transport 不得在一个临时事件循环中创建后再交给另一个事件循环。

```text
main()
→ asyncio.run(run_application())
→ 加载 Provider、Prompt、安全和 MCP 配置
→ 创建核心 ToolRegistry
→ 创建 McpConnectionManager
→ 并发激活全部 enabled server
→ 按稳定顺序注册远端 Tool adapter
→ 创建 AgentLoop 与 ChatApp
→ await ChatApp.run_async()
→ finally: await McpConnectionManager.close()
```

### 6.2 Required 与 optional

- 任一 required server 激活失败：取消其他未完成激活，关闭已经打开的连接和子进程，应用启动失败；
- optional server 激活失败：记录脱敏 warning，跳过该 server，其他 server 和内置工具继续可用；
- 配置结构错误不是 optional 运行故障，始终使应用启动失败；
- warning 只包含 server ID、稳定错误码和安全消息，不包含环境变量值、header、完整响应正文或子进程环境。

### 6.3 会话级连接缓存

`McpConnectionManager` 以精确 server ID 为键保存活动 client。一个应用进程内，每个 server 最多一个活动 MCP session：

- stdio：一个长期存在的子进程；
- Streamable HTTP：一个长期存在的 HTTP client、可选 MCP session ID 和至多一个独立 GET SSE listener；
- 多个并发调用复用同一 client；
- 不跨应用重启复用连接或工具缓存。

## 7. JSON-RPC 2.0 消息层

### 7.1 支持的消息形态

只接受单个 JSON object，不接受 JSON-RPC batch array。消息必须精确包含 `jsonrpc: "2.0"`，并归类为：

- request：有非空 `method` 和非空 `id`；
- notification：有非空 `method` 且没有 `id`；
- success response：有 `id` 和 `result`；
- error response：有 `id` 和包含整数 `code`、字符串 `message` 的 `error`。

`result` 与 `error` 必须互斥。ID 只能是字符串或整数，拒绝 `null` 和布尔值。

### 7.2 Request ID 与异步匹配

- client request ID 在每个 MCP session 中从整数 `1` 开始单调递增；
- 同一 session 不复用 ID，包括超时或已取消的 ID；
- pending map 精确为 `id → Future`；
- response 按 ID 完成对应 Future，允许响应顺序与发送顺序不同；
- 未知 ID、重复 response 或已经完成的 ID 记为协议错误，不得错误完成其他请求；
- transport 关闭时，所有 pending Future 使用同一个连接关闭错误完成并清空。

stdio 的所有写操作通过单 writer lock 串行化，保证一条 JSON 消息及其换行不可交错。HTTP POST 可以并发；入站 JSON 与所有 SSE stream 最终进入同一个消息路由器。

### 7.3 Server 发起的消息

由于本章发送空 client capabilities：

- 收到 `ping` request 时返回空 `result`；
- 收到其他 server request 时返回 JSON-RPC `-32601 Method not found`；
- 收到 `notifications/tools/list_changed` 时标记并刷新工具快照；
- 收到 progress、logging 或未知 notification 时可做脱敏诊断后忽略；
- notification 永远不返回 response。

### 7.4 超时与取消

每个 client request 都有明确超时。除 `initialize` 外，请求超时或外层 task 被取消时：

1. 从 pending map 移除该 request；
2. 尽力发送 `notifications/cancelled`，其中 `requestId` 使用原 ID；
3. 忽略之后到达的迟到 response；
4. 不自动重试原请求。

现有 `ToolRegistry` 继续以 `RemoteMcpTool.timeout_seconds` 实施工具超时。adapter 捕获取消并触发上述 MCP cancellation；不得再叠加一个更短、行为不同的隐藏超时。

## 8. MCP 初始化与能力协商

### 8.1 初始化请求

transport 建立后，第一个 client request 必须是：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {
      "name": "mewcode-agent",
      "version": "<project version>"
    }
  }
}
```

`clientInfo.version` 从已安装项目元数据读取；不得另写一个可能漂移的版本常量。

### 8.2 初始化响应

client 必须验证：

1. response ID 与 initialize request 相同；
2. `protocolVersion` 精确为 `2025-11-25`；
3. `capabilities` 是 object；
4. `serverInfo.name` 和 `serverInfo.version` 是非空字符串；
5. `capabilities.tools` 存在且是 object。

server 返回其他协议版本时，client 关闭连接并返回 `unsupported_mcp_version`，不回退。server 没有 Tools 能力时返回 `mcp_tools_capability_missing`。

初始化成功后，client 必须先发送：

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized"
}
```

然后才能发送 `tools/list`。

### 8.3 Server 元数据

`serverInfo`、`instructions`、icons、capabilities 和未知扩展字段只进入内存诊断快照：

- 不拼入 Prompt；
- 不显示为安全依据；
- 不获取 icon URL；
- 不信任 annotations；
- 不把未知 capability 当作已实现能力。

## 9. stdio 传输

1. 使用 `create_subprocess_exec` 启动配置中的精确 `command` 与 `args`。
2. JSON-RPC 使用 UTF-8 单行 JSON；每条消息后写一个 `\n`。
3. 消息正文不得包含真实换行；JSON 字符串中的换行必须由 JSON 编码器转义。
4. stdout 只能包含 MCP 消息；空行可忽略，其他非 JSON 行视为 transport protocol error。
5. stderr 使用独立后台 task 持续 drain，避免子进程因 pipe 填满而阻塞。
6. stderr 仅进入有界、脱敏诊断，不把 stderr 的存在当作请求失败。
7. 子进程意外退出时，关闭 transport 并失败所有 pending request。
8. 关闭顺序严格为：关闭 stdin → 等待 `shutdown_timeout_seconds` → terminate → 再等待同一 timeout → kill → 回收进程。
9. reader、stderr task 和 pending Future 在所有退出路径上都必须结束，不留下后台进程或 task。

## 10. Streamable HTTP 传输

### 10.1 公共请求规则

每条 client JSON-RPC 消息使用一个新的 HTTP POST：

```text
Content-Type: application/json
Accept: application/json, text/event-stream
```

initialize 之后的所有请求还必须包含：

```text
MCP-Protocol-Version: 2025-11-25
```

如果 initialize response 返回 `MCP-Session-Id`，后续 POST、GET 和 DELETE 都必须携带该值。

### 10.2 POST response

- notification 或 response POST 被接受时，允许 HTTP `202` 且无正文；
- request POST 必须接受 `application/json` 或 `text/event-stream`；
- JSON body 必须是一个 JSON-RPC response；
- POST SSE 可以先携带相关 request/notification，最终必须携带原 request 的 response；
- 收到最终 response 后关闭该 POST stream；
- HTTP 状态、Content-Type 和 JSON-RPC 消息分别校验，不能把 HTML 错误页交给 JSON parser。

### 10.3 GET SSE listener

初始化完成后 client 发起至多一个 GET：

```text
Accept: text/event-stream
```

- `200 text/event-stream`：启动长期 listener；
- `405 Method Not Allowed`：server 不提供独立 listener，连接仍有效；
- 其他状态：按 transport error 处理；
- GET stream 不得发送与新 client request 对应的普通 response，除非它是在恢复先前 stream。

### 10.4 SSE 解析与恢复

- 支持 `data`、`id` 和 `retry` 字段；
- 同一事件的多个 `data` 行按 SSE 规则连接后再解析 JSON；
- 空 `data` 事件可用于建立恢复点，不进入 JSON-RPC router；
- 保存每个 stream 最后一个 event ID；
- server 关闭未终止的 stream 时，按 `retry` 等待后使用 GET 和 `Last-Event-ID` 恢复；
- `retry` 只控制该 stream 重连，不导致 `tools/call` 自动重发；
- event ID 按 opaque string 保存，禁止解析、改写或跨 stream 复用。

### 10.5 Session 失效

携带 `MCP-Session-Id` 的请求收到 HTTP `404` 时：

1. 在 per-server reconnect lock 内废弃旧 session；
2. 重新 initialize；
3. 重新发现工具；
4. 仅在明确收到 `404`、server 按规范拒绝旧 session 的情况下，使用新的 JSON-RPC ID 重发原请求一次。

网络断开、timeout、无法解析的响应或 POST SSE 中途断开都不能证明 server 未执行工具，因此 `tools/call` 不自动重试。

### 10.6 HTTP 关闭

如果存在 session ID，正常关闭时发送携带 session ID 和协议版本的 HTTP DELETE：

- `2xx` 和 `405` 都视为关闭流程可继续；
- DELETE 失败只记录脱敏诊断，不阻止本地资源释放；
- 最后关闭 GET listener 和 HTTP client。

## 11. 工具发现与缓存

### 11.1 分页发现

初始化完成后发送无 cursor 的 `tools/list`。响应有非空 `nextCursor` 时，把该 opaque cursor 原样放入下一次请求，直到 `nextCursor` 缺失。

client 必须拒绝：

- `tools` 不是 list；
- 同一 server 跨页出现精确重复的工具名；
- `nextCursor` 类型错误；
- cursor 循环；
- 超过 `100` 页；
- 一个 server 超过 `512` 个工具。

不得解析或修改 cursor。

### 11.2 工具定义校验

每个可注册工具必须满足：

- `name` 是长度 `1–128` 的非空字符串，保持精确且区分大小写；
- `inputSchema` 是根 `type` 精确为 `object` 的有效 JSON Schema object；
- 无 `$schema` 时按 JSON Schema 2020-12；
- 显式 dialect 交给 JSON Schema validator 精确识别；不支持的 dialect 使该工具发现失败；
- 本地结果校验不自动获取远程 `$ref`，无法从当前 schema 本地解析的引用使结果校验失败；
- `description` 缺失时使用包含精确 server ID 和远端工具名的固定 fallback；
- `execution.taskSupport` 为 `required` 时跳过该工具并输出稳定 warning，因为本章不支持 Tasks；
- `forbidden`、`optional` 或缺失时只使用普通 `tools/call`；
- `outputSchema` 存在时必须先验证 schema 本身，并在调用成功后验证 `structuredContent`。
- `outputSchema` 的根 `type` 必须精确为 `object`。

### 11.3 稳定注册顺序

内置工具保持现有顺序。MCP 工具按以下顺序追加：

1. server ID 的 Unicode code point 顺序；
2. 同一 server 内远端工具原名的 Unicode code point 顺序。

不使用网络完成顺序或 `tools/list` 原始分页顺序，避免 Provider 工具数组和 Prompt 缓存前缀无谓漂移。

### 11.4 工具列表变更

仅当 server 声明 `capabilities.tools.listChanged: true` 时接受 `notifications/tools/list_changed`：

1. 同一 server 的并发通知合并为一个刷新 task；
2. 重新读取全部分页；
3. 完整校验新快照；
4. 成功后一次性替换该 server 的 adapter 集合；
5. 失败时保留旧快照并记录脱敏 warning。

`ToolRegistry` 需要新增按 server 原子替换 MCP 工具的方法。替换不得修改内置工具。已发送给模型但刷新后被删除的工具若仍被调用，返回 `tool_not_found`，不得路由到其他同名工具。

## 12. 本地工具别名

MCP 工具名允许大小写、连字符和点，而当前安全模型只接受 `[a-z][a-z0-9_]*`。因此不直接修改、猜测或规范化远端标识符，而是维护精确映射：

```text
digest_input = UTF8(server_id + "\0" + remote_tool_name)
digest = SHA256(digest_input).hexdigest()[0:24]
local_name = "mcp_" + server_id + "_" + digest
```

性质：

- `local_name` 始终匹配现有工具名约束；
- 同一 server ID 与精确远端名在重启后得到相同别名；
- 大小写不同的远端名得到不同 digest；
- 不把 `-`、`.`、大小写或 Unicode 猜测性转换为另一名称；
- adapter 内保存 `local_name → (server_id, remote_tool_name)`；
- 注册时检测完整别名冲突；发生冲突立即失败，不通过追加序号解决；
- `mcp_` 前缀保留给本子系统，内置或其他扩展工具不得使用。

adapter 的 description 必须首先标明精确 server ID 与远端工具名，使模型和审批界面能理解来源；实际 `tools/call.params.name` 始终使用原始远端名。

## 13. `Tool` 适配与安全策略

`RemoteMcpTool` 实现现有接口：

| `Tool` 字段 | 来源 |
| --- | --- |
| `name` | 第 12 节稳定本地别名 |
| `description` | 来源前缀加远端 `description` |
| `parameters` | 远端 `inputSchema` 原样深拷贝 |
| `category` | `tool_categories` 精确覆盖，否则 `command` |
| `timeout_seconds` | server 的 `request_timeout_seconds` |
| `execute()` | 调用 manager 的精确 server 与远端工具 |

安全约束：

1. 远端工具和 annotations 都视为不可信输入。
2. 默认 `command` 保证 Chapter 04 `default` 模式下调用需要审批。
3. read 分类才可以进入现有连续读并发批次。
4. write 和 command 保持现有串行屏障。
5. 现有 session/project/user 安全规则通过稳定本地别名匹配。
6. 当前请求的已批准计划继续遵守 Chapter 04 的既有语义；本章不暗中改变授权顺序。
7. 永久审批指纹只存稳定别名和调用参数，不保存 HTTP header、stdio env 或 server secret。
8. Server instructions 和工具结果永远不能修改安全策略或绕过审批。

## 14. `tools/call` 与结果映射

### 14.1 请求

adapter 把已经由 Registry 解析为 object 的参数发送为：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "<精确远端工具名>",
    "arguments": {}
  }
}
```

不把本地别名发送给 server。不添加 task 字段。

### 14.2 成功结果

`isError` 缺失时按 `false` 处理。成功结果归一化为可 JSON 序列化 object：

```text
server_id
remote_tool_name
content
structured_content
```

- `content` 保留标准 text、image、audio、resource_link 和 embedded resource object；
- `structured_content` 来自精确字段 `structuredContent`；
- `_meta` 只保留在连接内部，不写入 `ToolResult`、模型历史或普通日志；
- 不主动获取 resource link、embedded resource URI 或 icon；
- `outputSchema` 存在时，`structuredContent` 缺失或校验失败返回 `mcp_invalid_tool_result`；
- 归一化后的完整结果超过 `4 MiB` 时返回 `mcp_result_too_large`，不写入历史。

### 14.3 Tool execution error

`isError: true` 映射为失败 `ToolResult`：

```text
error_code = "mcp_tool_error"
error_message = 从 text content 提取并限制长度的安全消息
details = 经过大小限制的标准 content 与 structuredContent
```

该错误回填模型，使模型可以根据远端业务或参数错误自我修正。

### 14.4 Protocol error

JSON-RPC error 映射为：

```text
error_code = "mcp_protocol_error"
error_message = 包含整数 code 与长度受限的 message
```

不把任意 `error.data` 直接写入日志或模型历史。transport、连接、schema 和生命周期错误使用各自稳定错误码，不伪装为远端工具业务错误。

## 15. 并发、断线与重连

### 15.1 并发请求

一个 client 必须允许多个 pending request：

- read 类 MCP 工具可由现有 Scheduler 并发提交；
- stdio 写入串行但等待响应并发；
- HTTP 使用并发 POST；
- response 只按 JSON-RPC ID 关联；
- 不假设先发请求先返回。

### 15.2 断线前后边界

- 请求尚未写入 transport 前发现连接无效：允许在 reconnect lock 内重新初始化，再发送一次；
- 请求已经写入后发生断线：返回 `mcp_connection_lost`，不得自动重发；
- 下一个新调用可以重新建立连接和工具快照；
- 重连后目标远端工具已不存在时返回 `mcp_tool_not_found`；
- required 只控制启动，不把运行期断线升级为整个 Agent 进程退出。

### 15.3 防止重连风暴

每个 server 只有一个 reconnect lock。同一时刻只有一个 task 可以建立新连接；其他调用等待该结果。一次调用最多触发一次重连，不做无限循环或后台指数重试。

## 16. 固定资源限制

本章使用以下初始上限：

| 项目 | 上限 |
| --- | --- |
| 单条 JSON-RPC 或 SSE `data` 消息 | `8 MiB` UTF-8 bytes |
| 单 server 工具数量 | `512` |
| `tools/list` 页数 | `100` |
| 归一化工具结果 | `4 MiB` UTF-8 JSON |
| 单条安全错误消息 | `2 KiB` UTF-8 bytes |
| stdio 诊断 ring buffer | `256 KiB` UTF-8 bytes |

超限必须产生稳定错误并释放正在累积的 buffer，不能继续无界读取。日志截断按 UTF-8 边界进行，且不得包含 secret。

## 17. 错误模型

至少定义以下稳定错误码：

| 错误码 | 含义 |
| --- | --- |
| `mcp_config_error` | YAML、字段、类型、环境引用或 URL 配置无效 |
| `mcp_connect_failed` | transport 无法建立 |
| `unsupported_mcp_version` | server 未接受 `2025-11-25` |
| `mcp_tools_capability_missing` | server 未声明 Tools 能力 |
| `mcp_protocol_error` | JSON-RPC error response 或协议违规 |
| `mcp_request_timeout` | 非工具生命周期请求超时 |
| `mcp_connection_lost` | 已发送请求期间连接丢失 |
| `mcp_tool_not_found` | 重连或刷新后远端工具不存在 |
| `mcp_tool_error` | `tools/call` 返回 `isError: true` |
| `mcp_invalid_tool_result` | 工具结果结构或 output schema 无效 |
| `mcp_result_too_large` | 工具结果超过上限 |
| `mcp_message_too_large` | transport 消息超过上限 |
| `mcp_shutdown_failed` | 关闭过程需要强制终止或未干净完成 |

面向模型的工具错误继续由 `ToolResult` 表达。启动期错误由 CLI 输出安全消息。内部异常、traceback、header、env、session ID 和任意远端原始大正文不得直接暴露。

## 18. 测试策略

### 18.1 配置测试

- 文件缺失返回空 server 集合；
- 重复键、未知字段、错误联合结构和 bool timeout 被拒绝；
- server ID、transport、URL、cwd、env 和 header 精确校验；
- disabled 项不读取 env；
- secret 不进入 repr、错误或捕获输出；
- 项目级 MCP 配置不会被读取。

### 18.2 JSON-RPC 测试

- 单调 ID、乱序 response 和并发 pending request；
- 重复、未知和错误类型 ID；
- result/error 互斥；
- batch array 拒绝；
- notification 不产生 response；
- ping 返回空 result；
- 未声明 server request 返回 `-32601`；
- timeout/cancel 后迟到 response 被忽略。

### 18.3 stdio fake server

- initialize 必须是第一条 request；
- initialized 必须先于 tools/list；
- 多页工具发现；
- 多请求乱序响应；
- stderr 持续输出不阻塞 stdout；
- stdout 非协议文本导致连接失败；
- 进程提前退出完成全部 pending Future；
- 正常关闭、terminate 和 kill 三条路径都回收进程。

### 18.4 Streamable HTTP fake server

- POST JSON response；
- POST SSE response；
- notification 的 `202`；
- GET `405`；
- GET SSE server request/notification；
- `MCP-Protocol-Version: 2025-11-25`；
- session ID 在后续 POST/GET/DELETE 中精确复用；
- SSE `id`、`retry`、断线和 `Last-Event-ID`；
- session `404` 后只重新初始化并重发一次；
- 普通网络断线不重发 tools/call；
- redirect、错误 Content-Type、HTML error body 和超大消息被拒绝；
- header/env/session secret 不进入日志。

### 18.5 工具适配与安全

- 稳定别名使用精确 server ID 和远端名计算；
- 大小写不同的远端工具名得到不同别名；
- 别名冲突硬失败；
- annotations 不改变 category；
- 默认 command 进入审批；
- 精确 `tool_categories` 覆盖生效；
- read 工具沿用连续读并发；
- `taskSupport: required` 工具不注册；
- local alias 调用被还原为精确 remote name；
- 成功、`isError`、JSON-RPC error、output schema 错误和超限结果准确映射。

### 18.6 生命周期回归

- required/optional server 的启动差异；
- 多 server 并发激活但注册顺序稳定；
- list_changed 原子替换单一 server 工具；
- MCP 失败不改变内置工具；
- 应用退出后没有活动子进程、HTTP connection、reader task 或 pending Future；
- Chapter 01–04 默认测试继续通过。

## 19. 验收标准

1. MewCode 只发送和接受 MCP `2025-11-25` 会话，不隐式降级。
2. 同一套 client lifecycle 同时运行在 stdio 和 Streamable HTTP 上。
3. initialize、initialized、分页 tools/list 和 tools/call 的顺序与字段准确。
4. 多个并发 request 可以按 ID 正确接收乱序 response。
5. 每个 server 在会话内只创建一个活动 client，并被后续工具调用复用。
6. 远端工具通过稳定别名进入现有 Provider 工具列表，调用时恢复精确远端名称。
7. 远端工具默认按 command 接受 Chapter 04 安全策略和 HITL。
8. Server instructions、annotations 和远端正文不能获得系统权限或绕过安全决策。
9. stdio 不经过 shell，环境变量按显式映射传递，退出时子进程被回收。
10. Streamable HTTP 正确处理 JSON/SSE、版本头、session、GET listener 和恢复游标。
11. 不对可能已执行的远端工具调用做自动重试。
12. 配置、日志、异常、历史和 repr 中不泄漏 header、env、session ID 或 secret。
13. 工具列表变化只原子替换所属 server，不扰动内置工具和其他 server。
14. 所有 buffer、分页、工具数量、结果和错误消息都有固定上限。
15. 默认测试不访问公网、不要求真实 MCP server，且 Chapter 01–04 全部回归通过。

## 20. 已确认的实现决策

本章按以下已确认选择完成实现：

1. 只支持稳定版 `2025-11-25`，不兼容旧协议和 RC；
2. 只实现 Tools，其他 MCP 能力全部不声明；
3. 在项目内实现受限协议客户端，不依赖 MCP SDK 隐式管理生命周期；
4. MCP 配置只允许位于用户全局目录，不读取项目 MCP 配置；
5. 远端工具默认安全分类为 command；
6. 使用 hash 型稳定本地别名，不改写远端工具名；
7. HTTP OAuth 不在本章，通过环境变量只能提供预置完整 header；
8. Tasks 和 `taskSupport: required` 工具不支持；
9. required server 失败会阻止应用启动，optional server 失败会被跳过。
