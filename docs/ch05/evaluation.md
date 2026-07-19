# Chapter 05 Evaluation：MCP 工具客户端与双传输接入

## 验收环境

| 字段 | 实际值 |
| --- | --- |
| 时间 | `2026-07-19T23:48:26+08:00` |
| 分支 | `master` |
| 验收基线 Commit | `369f7c132ca846382d85a9e3bda65ab830fab827` |
| MCP 协议版本 | `2025-11-25` |
| 代码状态 | 最终审计与文档改动尚未提交 |

## 自动化结果

| 检查 | 实际结果 |
| --- | --- |
| `uv run pytest -q` | `482 passed, 1 skipped in 14.54s`，exit code `0` |
| Chapter 05 与 CLI 聚焦测试 | `154 passed in 6.58s`，exit code `0` |
| `uv run python -m compileall -q src tests integration_tests` | exit code `0` |
| `git diff --check` | exit code `0`；只有 Git 的 LF→CRLF 工作区提示 |
| 新增/修改 Python 与 Markdown 尾随空白扫描 | 无匹配，`rg` exit code `1` |

唯一跳过项仍是 `tests/test_security_boundary.py::test_path_sandbox_rejects_symlink_escape`：当前 Windows 环境不允许测试创建目录符号链接。本章新增测试没有跳过项。

## 已验证场景

- 配置只读取用户全局 `mcp_servers.yaml`，项目 MCP 配置不会被加载；
- 严格拒绝重复 YAML key、未知字段、错误联合结构、bool timeout、越界 cwd、无效 URL 和保留 header；
- stdio 子进程不经过 shell，环境变量不隐式继承，stdout 按 UTF-8 换行帧解析，stderr 持续有界 drain；
- 真实本地 stdio fake server 已贯穿 `McpConnectionManager`、`ToolRegistry`、`RemoteMcpTool` 和 `tools/call` 完成端到端回显，并在关闭后回收子进程；
- Streamable HTTP 的 POST JSON、POST SSE、GET SSE、`202`、`405`、session/version header、DELETE 和 `Last-Event-ID` 已通过 `httpx.MockTransport` 本地测试；
- POST SSE 断开后使用独立 GET 和原 stream 游标恢复，不重新 POST 原工具调用；
- JSON-RPC request ID 从 `1` 单调递增，并发请求可按 ID 接收乱序响应；
- timeout/cancel 发送 `notifications/cancelled`，迟到 response 被忽略，关闭时所有 pending request 使用同一错误完成；
- initialize、initialized、分页 tools/list 和 tools/call 的顺序及字段通过测试；
- 工具 schema 使用 JSON Schema 2020-12 默认 dialect，拒绝未知 dialect、非 object 根 schema 和自动远程 `$ref` 获取；
- `taskSupport: required` 工具被跳过，`forbidden`、`optional` 和缺失值使用普通 tools/call；
- 远端工具别名按精确 server ID、NUL 分隔符和远端原名计算 SHA-256；大小写不同的远端名不会被转换或合并；
- 远端工具默认分类为 `command`，精确 `tool_categories` 覆盖生效，annotations 不参与安全分类；
- required server 失败会取消并清理其他激活；optional server 失败会被跳过并生成脱敏诊断；
- 每个 server 的 client 在会话内复用；并发 session 404 共享同一重建，原请求至多重发一次；
- 普通网络断线不会自动重发可能已执行的 tools/call，下一个新调用才建立新连接；
- list-changed 通知合并刷新，成功时按 server 原子替换，失败时保留旧工具快照；
- `_meta`、HTTP header、stdio env、session ID、JSON-RPC error data 和 server instructions 不进入普通工具结果或模型历史；
- 消息、工具数量、分页、结果、错误文本和 stderr buffer 的固定上限通过测试；
- CLI 的 MCP 激活、Textual `run_async()` 和关闭运行在同一事件循环，启动失败及界面异常路径都会清理 manager；
- Chapter 01–04 默认测试继续通过。

## 未连接的外部系统

默认验收没有访问公网，也没有连接用户真实 MCP server。Streamable HTTP 使用本地内存 transport 验证 HTTP/SSE 语义；stdio 使用本机 Python 子进程进行真实端到端验证。OAuth、Resources、Prompts、Tasks、旧 HTTP+SSE 和旧 MCP 协议版本均不在本章范围内。
