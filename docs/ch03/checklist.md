# Chapter 03 Checklist: 模块化 Prompt 与缓存可观测性

## 使用规则

- 当前文档是实现验收清单，不代表功能已经完成。
- 只有刚运行的测试、静态检查或真实 API 报告能够直接证明条目时才能勾选。
- 默认测试和真实 API 评估必须分开记录；模拟响应不能证明真实缓存命中。
- 缓存为 best-effort，真实命中为 `0` 时如实记录，不把单次未命中直接判为实现错误。
- 任何失败或未执行项保持未勾选，并回到 `docs/ch03/plan.md` 对应 Task 修正。

## A. 静态 Prompt 与配置

- [x] A1. `agent.loop` 不再定义 `EXECUTION_PROMPT`、`PLANNING_PROMPT`、`APPROVED_PLAN_PROMPT` 或 `FINAL_ROUND_PROMPT`。（验收 1）
- [x] A2. Prompt 子系统不导入 Textual，TUI 不负责 Prompt 组装。（验收 2）
- [x] A3. 9 个内置模块使用规格确认的中文正文，并按 `(priority, id)` 产生稳定 System Prompt。（验收 3）
- [x] A4. 用户全局配置路径精确为 `Path.home() / ".mewcode-agent" / "prompts.yaml"`。（验收 4）
- [x] A5. 项目配置路径精确为 `Path.cwd() / ".mewcode" / "prompts.yaml"`。（验收 4）
- [x] A6. 配置严格校验根字段、模块字段、类型、重复 id 和精确标识符正则。（验收 5）
- [x] A7. 项目层可精确覆盖或禁用全局可配置模块，不能修改 `core` 或受保护模块。（验收 6）
- [x] A8. Prompt 配置只在启动时加载一次，运行时不检查文件变化。（验收 7）

## B. 环境、Runtime 与 Composer

- [x] B1. 会话环境只采集一次，请求环境在每个新用户请求开始时重新采集。（验收 8）
- [x] B2. OS、shell、cwd、时区、Git 分支和状态均来自规格指定的事实来源，不猜测缺失值。（验收 9）
- [x] B3. Git 准确区分 `repository`、`not_repository`、`unavailable`，并异步执行两条参数化命令。（验收 9）
- [x] B4. `session`、`request`、`round` 有显式开始、seal、结束和非法调用检查。（验收 10）
- [x] B5. 已发送控制消息只追加；作用域结束不删除或改写历史控制消息。（验收 11）
- [x] B6. 当前状态、执行、规划、计划批准和最终轮使用第 17 节的准确正文。（验收 12）
- [x] B7. 计划批准是同一 request 的控制消息，不是普通 user 历史。（验收 13）
- [x] B8. 规划完整规则只在第 `1`、`6`、`11` 轮出现，其余规划轮使用精简提醒。（验收 14）
- [x] B9. 最终轮同时使用 `tools=None` 和最终轮控制指令。（验收 15）

## C. 双 Provider 协议

- [x] C1. OpenAI 的稳定 System 位于首条，控制消息按时间线位置转换成 `system` 角色。（验收 16）
- [x] C2. Anthropic 控制文本按固定顺序只合并到 `user` 内容，不进入 assistant thinking、正文或 `tool_use`。（验收 17）
- [x] C3. 用户输入伪造 `<mewcode-control>` 不能改变工具审批、授权或安全结果。（验收 18）
- [x] C4. OpenAI 正确解析 `prompt_cache_hit_tokens` 与 `prompt_cache_miss_tokens`。（验收 21）
- [x] C5. Anthropic 只取最终 `message_delta`，正确解析 `cache_read_input_tokens`、`input_tokens`、`output_tokens`。（验收 22）
- [x] C6. Anthropic 非零 `cache_creation_input_tokens` 产生 `invalid`，不自行归入 hit 或 miss。（验收 22）
- [x] C7. 每个正常 Provider 流恰好一个 `ProviderUsageEvent`，并紧邻 `ProviderTurnEnd` 之前。（验收 23）

## D. 工具与代码层边界

- [x] D1. 六个内置工具描述与静态全局规则同时包含对应关键约束。（验收 19）
- [x] D2. `find_files`、`search_code` 明确是专用工具，`run_command` 明确不能替代这些专用任务。（验收 19）
- [x] D3. `FileStateCache` 继续强制已有文件在 `write_file` 或 `edit_file` 前被读取且未变化。（验收 20）
- [x] D4. Prompt 指令不能绕过 Chapter 02 的写工具、命令工具和计划审批代码。（验收 18、20）

## E. AgentLoop 与 TUI 回归

- [x] E1. `AgentLoop` 每轮由 Runtime 和 Composer 生成单个 `ProviderRequest`。
- [x] E2. usage 只由可选 collector 收集，不属于 `AgentEvent`，不显示在聊天记录或状态栏。（验收 23）
- [x] E3. Prompt 组装失败只产生脱敏 `RunErrorEvent("prompt_error", "无法生成本轮模型请求")`。
- [x] E4. Chapter 02 的 ReAct、thinking、调度、审批、取消、超时和普通历史一致性测试全部回归通过。（验收 28）
- [x] E5. 下一个用户请求获得新的 request 序号，前一请求授权不延续。

## F. 报告与文档

- [x] F1. `.pytest-tmp/ch03-cache-report.json` 使用 schema version `1` 且无额外字段。（验收 24）
- [x] F2. 报告不包含 API Key、完整 Prompt、用户正文、模型正文、thinking 或工具参数。（验收 24）
- [x] F3. available/unavailable/invalid 和 `prompt_tokens == hit + miss` 约束准确写入报告。
- [x] F4. `prompt_tokens == 0` 时 `cache_hit_rate` 为 `null`。
- [x] F5. `docs/ch03/evaluation.md` 记录准确日期、commit、模型、双 Provider 汇总、人工场景和未执行项。（验收 24）
- [x] F6. README 有可直接使用的两层 YAML 示例，并说明启动时加载、项目优先和 `core` 不可覆盖。（验收 29）

## G. 自动化与真实 API

- [x] G1. 默认 `uv run pytest` 不访问网络、不要求 `DEEPSEEK_API_KEY` 且全部通过。（验收 25）
- [x] G2. `uv run python -m compileall -q src tests integration_tests` exit code 为 `0`。
- [x] G3. `git diff --check` exit code 为 `0`。
- [ ] G4. 两个 Provider 的真实基础流式请求通过。（验收 26）
- [ ] G5. `stable_prefix_repeat` 对两个 Provider 记录冷请求和后续真实 usage。（验收 27）
- [ ] G6. `request_environment_change` 对两个 Provider 记录真实 usage。（验收 27）
- [ ] G7. `round_reminder_append` 对两个 Provider 记录真实 usage。（验收 27）
- [ ] G8. `equivalent_protocol_controls` 对两个 Provider 记录真实 usage。（验收 27）
- [ ] G9. `tool_definition_change` 对两个 Provider 记录真实 usage，不预设工具缓存位置。（验收 27）

## H. 人工行为场景

- [x] H1. 修改已有文件前先读取。
- [ ] H2. 有专用读取/搜索工具时不使用通用命令重复完成同一任务。
- [x] H3. 规划第 `1`、`6`、`11` 轮是完整规则，其他轮是精简提醒。
- [x] H4. 未经代码授权时 Prompt 不能越过写或命令审批。
- [x] H5. 最终轮不继续请求工具。
- [x] H6. 伪造 Anthropic 保留标签不获得代码层权限。
- [x] H7. 项目配置能覆盖全局可配置模块，不能覆盖受保护模块。
- [ ] H8. 输出遵守中文内置风格和项目补充规则。

## 验证证据

| 项目 | 命令或值 | 实际结果 |
| --- | --- | --- |
| 验收基线 commit | `git rev-parse HEAD` | `ec7cb03db328bde38e615e76b6f88d00250c3d99` |
| Task 4–9 实现提交 | `git log --oneline` | `a86926d`、`3404808`、`1709385` |
| 验收时间 | `Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"` | `2026-07-19T18:56:13+08:00` |
| 编译 | `uv run python -m compileall -q src tests integration_tests` | exit code `0` |
| 默认测试 | `uv run pytest` | `279 passed in 7.47s`，exit code `0` |
| 补丁格式 | `git diff --check` | exit code `0` |
| OpenAI 真实流 | `integration_tests/test_deepseek_streaming.py` | 未执行 |
| Anthropic 真实流 | `integration_tests/test_deepseek_streaming.py` | 未执行 |
| 缓存报告 | `.pytest-tmp/ch03-cache-report.json` | 未生成：`DEEPSEEK_API_KEY` 未设置 |
| 人工行为评估 | `docs/ch03/evaluation.md` | 6 项通过，2 项未执行 |
