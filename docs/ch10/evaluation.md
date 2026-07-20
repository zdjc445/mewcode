# Chapter 10 Evaluation：声明式 Hook 规则引擎

## 验收环境

| 字段 | 实际值 |
| --- | --- |
| 时间 | `2026-07-21T01:25:52.3049278+08:00` |
| 分支 | `master` |
| 验收代码基线 Commit | `66cd816f4ecaabfa377aff8f9b00b4fe49d52fbd` |
| 真实 API 条件 | `DEEPSEEK_API_KEY` 未设置 |
| 代码状态 | 本验收记录修正尚未提交，其余 Chapter 10 代码、测试与文档已提交并推送 |

## 分批提交记录

| Commit | 内容 |
| --- | --- |
| `256bbcd` | Chapter 10 声明式 Hook 规格 |
| `c96df81` | Hook 模型、严格两层 loader、matcher、模板、四类动作和运行引擎 |
| `13dae00` | Agent/会话/压缩/工具/CLI 生命周期集成、README 与回归测试 |
| `2e7d734` | Chapter 10 验收记录与 shell 取消收尾测试 |
| `66cd816` | 修正可选 condition、all/any 组合和缺失模板变量契约 |

## 自动化结果

| 检查 | 实际结果 |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -q -rs` | `780 passed, 4 skipped in 23.88s`，exit code `0` |
| Hook/Agent/CLI 聚焦测试 | `95 passed in 14.01s`，exit code `0` |
| `.venv\Scripts\python.exe -m compileall -q src tests integration_tests` | exit code `0` |
| `git diff --check` | exit code `0`；只有 Git 的 LF→CRLF 工作区提示 |
| `uv build --wheel` | 成功生成 `mewcode_agent-0.1.0-py3-none-any.whl` |

四个跳过项分别是：

- `tests/test_instruction_loader.py::test_symlink_outside_root_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_notes_storage.py::test_project_notes_symlink_escape_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_security_boundary.py::test_path_sandbox_rejects_symlink_escape`：当前 Windows 环境不允许测试创建目录符号链接；
- `tests/test_session_storage.py::test_session_permissions_are_private_on_posix`：只适用于 POSIX 权限契约。

## 已验证场景

- 用户 `~/.mewcode-agent/hooks.yaml` 与项目 `.mewcode/hooks.yaml` 使用严格 UTF-8 YAML、重复键检查、精确字段和 `version: 1`；不存在文件为空层；
- 项目规则按声明顺序先执行，并完整覆盖同 ID 用户规则；同层重复 ID、无效事件、非法 action/matcher/intercept 组合在启动时 fail-fast；
- 配置错误定位到精确层级和 `rules[index].field`，不包含 shell command、Prompt、HTTP 数据或工具参数正文；
- matcher 使用相同 `{kind, pattern}` 结构，类型敏感 `exact`、大小写敏感 `glob`、完整值 `regex` 和最大八层递归 `not` 均有边界测试；
- condition 可省略表示无条件触发；存在时必须且只能选择非空 `all` 或 `any`，混用和空组在加载时拒绝；
- 缺失条件字段在当前位置固定为 false；`not` 不会把缺失字段变成成功；字段和工具参数键不转换大小写、不猜测别名；
- `${...}` 支持字符串原文和紧凑 JSON 值；未知字段逐个替换为空字符串，非法路径、未闭合占位符和不可序列化值保持严格边界；
- `file.path` 只在工具参数 JSON object 含精确字符串键 `path` 时生成，不从其他键推断；
- shell 动作固定使用项目 cwd，Windows 通过 PowerShell、POSIX 通过 `/bin/sh`；成功、非零退出、取消终止和超时错误边界已覆盖；
- HTTP 动作使用共享 `httpx.AsyncClient`、绝对 HTTP(S) URL、模板 header/body、禁止重定向和 2xx 判定；响应正文不进入模型；
- Prompt 动作在活动 request 中注入唯一 request control，无活动 request 时 FIFO 排队；session reset 丢弃旧 session pending，同时保留尚未消费的 startup Prompt；
- Prompt Hook 不写 `ConversationHistory`、session JSONL、上下文摘要或自动笔记；
- subagent 配置完整解析并稳定返回 `hook_subagent_unavailable`，没有伪装成已执行；
- 同步规则按序等待；异步规则集中登记；once 在首次成功进入调度时消耗，动作失败不自动重试；
- 引擎 close 只分发一次 shutdown，等待活动后台任务各自完成或超时，关闭 HTTP client，清空 pending Prompt，并且重复 close 幂等；
- Hook matcher、模板、动作和诊断 handler 失败不改变 Agent 的原始成功、错误或取消结果，也不会递归触发 system error；
- CLI 在进入 UI 前依次发出 `system.startup` 与初始 `session.started`，退出时发出 `session.ended` 与 `system.shutdown` 后再关闭其他资源；
- `/clear` 和 `/resume` 只有在 SessionManager 成功提交切换后才发旧 session ended 与新 session started；切换失败保持旧 session 且不误发事件；
- Agent Loop 发出 user message before-send、完整 Provider turn after-receive、round started/ended 和所有稳定 RunError 对应的 system error；
- round outcome 对成功、继续、失败和取消使用精确值；Provider 非法流不会伪造 after-receive；
- 自动与手动整体摘要在真实 summary 调用前 await before-compaction，在成功、失败或取消后发 after-compaction；仅工具结果外置不触发整体摘要 Hook；
- ToolScheduler 在安全策略和审批通过后、ToolRegistry 调用前分发 before-execute；首个声明式 deny 返回 `tool_blocked_by_hook`，handler 不执行，after-execute 仍收到拒绝结果；
- 工具 after Hook 只观察结构化结果并逐字符返回原 `ToolResult`，不能修改工具名、参数或结果；
- README 明确列出配置路径、完整示例、动作语义和项目 Hook 可执行 shell/HTTP 的信任边界；
- Chapter 01–09 默认测试全部继续通过，会话、笔记和 Hook 配置没有新增自动清理路径。

## 未连接的外部系统

本次默认验收没有调用 DeepSeek API，因为 `DEEPSEEK_API_KEY` 未设置。Agent lifecycle、Prompt control、压缩、工具调用、会话切换和错误流使用本地确定性 Provider、Prompt runtime、SessionManager 与 Tool 替身验证。

HTTP 动作使用 `httpx.MockTransport` 验证请求 method、URL、header、body、状态码与不跟随重定向；没有访问公网。shell 动作在本机 PowerShell 中只执行 `exit 0` 与 `exit 7`，没有读写用户文件。没有读取或执行用户主目录中的真实 Hook 配置。
