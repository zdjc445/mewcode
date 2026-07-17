# Chapter 02 Checklist: ReAct Agent 循环与事件流

## 使用规则

- 当前文档是实现验收清单，不代表功能已经完成。
- 只有刚运行的测试或静态检查能够直接证明条目时才能勾选。
- 验证证据必须记录实际命令、exit code 和输出统计，不能根据预期填写。
- 任何失败项都必须保持未勾选，并回到 `docs/ch02/plan.md` 对应 Task 修正。

## A. 架构边界

- [x] A1. `src/mewcode_agent/agent/` 不导入 Textual。
- [x] A2. `ChatApp` 中不存在 `_run_agent_loop()`、`_stream_round()` 或其他 ReAct 主循环。
- [x] A3. `ChatApp` 不直接调用 `LLMProvider.stream_chat()` 或 `ToolRegistry.execute()`。
- [x] A4. CLI 只负责组装 Provider、Registry、History、AgentLoop 和 ChatApp。

## B. Provider 与 thinking 协议

- [x] B1. OpenAI 和 Anthropic Provider 都只输出 `ProviderStreamEvent`。
- [x] B2. 每轮恰好一个 `ProviderTurnEnd`，并且它位于 Provider 流末尾。
- [x] B3. 两个 Provider 都把停止原因归一化为 `end_turn`、`tool_calls`、`max_tokens` 或 `other`。
- [x] B4. OpenAI 只从真实 `delta.reasoning_content` 产生 thinking，不合成 thinking。
- [x] B5. OpenAI 工具调用轮把完整 thinking 作为 `reasoning_content` 回传。
- [x] B6. Anthropic 只从真实 thinking block/delta 产生 thinking，不合成 thinking。
- [x] B7. Anthropic 工具调用轮按原顺序回传 thinking block，并保留原始 `signature`。
- [x] B8. 无工具调用轮的完整 thinking 不进入 `ConversationHistory`。
- [x] B9. 工具调用轮的 thinking 只进入 `ChatMessage.thinking_blocks`，不混入 assistant `content`。
- [x] B10. 两个 Provider 都使用各自协议的准确 system prompt 参数。
- [x] B11. Provider 错误文案不包含 API Key、完整请求头或 SDK 原始对象。

## C. ReAct 循环与历史

- [x] C1. 普通无工具请求只调用 LLM 一次并产生一个 `FinalResponseEvent`。
- [x] C2. assistant 工具调用历史先于对应 tool 结果历史。
- [x] C3. 每个工具结果使用原始 `tool_call_id`。
- [x] C4. 工具结果回填后触发下一轮 LLM。
- [x] C5. 多工具结果按模型原始调用顺序写入历史。
- [x] C6. 最终正文先写入 assistant 历史，再产生 `FinalResponseEvent`。
- [x] C7. Provider 流失败、取消或 LLM 超时时，不保存未完成的 assistant 轮。
- [x] C8. 一次 run 只产生 Final、Error、Cancelled 三类终止事件之一。
- [x] C9. 终止事件是 run 的最后一个事件。

## D. 轮数、超时与错误

- [x] D1. 单个用户请求最多调用 LLM `15` 次。
- [x] D2. 会话可以继续提交后续用户请求，不受前一请求的 15 轮计数限制。
- [x] D3. 规划与获批执行共用一个轮数计数器。
- [x] D4. 第 15 轮传 `tools=None` 并追加最终轮提示词。
- [x] D5. 第 15 轮仍返回工具调用时产生 `max_rounds_exceeded`，且工具不执行。
- [x] D6. 单轮 LLM 超过 `120.0` 秒时产生 `llm_timeout`。
- [x] D7. Provider 以 `max_tokens` 停止时产生 `max_tokens_reached`。
- [x] D8. 空响应与无效 Provider 流分别产生规格定义的 `empty_response` 或 `invalid_provider_stream`。
- [x] D9. 工具仍使用各自 `timeout_seconds`，默认值为 `30.0` 秒。

## E. 工具分类与调度

- [x] E1. `read_file`、`find_files`、`search_code` 的 category 为 `read`。
- [x] E2. `write_file`、`edit_file` 的 category 为 `write`。
- [x] E3. `run_command` 的 category 为 `command`。
- [x] E4. 相邻 read 工具真实并发启动。
- [x] E5. 并发 read 的结果按原始调用顺序输出。
- [x] E6. write 和 command 每次只执行一个，并形成前后批次的串行屏障。
- [x] E7. 未知工具在原位置产生 `tool_not_found`，并形成调度屏障。
- [x] E8. 工具失败产生 `ToolResultEvent`，不会直接终止 Agent。
- [x] E9. 默认 `ToolExecutionInterceptor` 不改变执行和结果。
- [x] E10. before/after 拦截接口能够分别阻止执行和转换结果。

## F. plan-only 与审批

- [x] F1. plan-only 开关默认关闭，并由 UI 跨请求保留当前值。
- [x] F2. plan-only 中 read 工具无需审批。
- [x] F3. plan-only 中每个 write/command 分别弹出工具审批卡片。
- [x] F4. `allow_once` 只放行当前 `call_id`，plan-only 保持开启。
- [x] F5. `reject` 不产生 started 事件，并回填 `tool_blocked_in_plan_mode`。
- [x] F6. 最终计划卡片提供执行、修改、拒绝三个选择。
- [x] F7. 修改计划必须携带非空反馈，并把反馈作为新 user 历史和事件。
- [x] F8. 批准最终计划后，当前请求中的 write/command 不再逐次审批。
- [x] F9. 最终计划临时授权在请求终止时失效。
- [x] F10. 下一条用户消息仍服从没有关闭的 plan-only 开关。
- [x] F11. 第 15 轮计划卡片禁用执行和修改，只允许结束当前请求。
- [x] F12. 审批事件只暴露 request ID，不向 UI 暴露 Future。
- [x] F13. 未知、过期或重复 request ID 被拒绝。

## G. 取消与 TUI

- [x] G1. 等待 LLM 时取消会立即停止当前 Provider 消费。
- [x] G2. 等待工具或计划审批时取消会立即结束等待。
- [x] G3. 已启动单工具在取消后完成或超时，再终止请求。
- [x] G4. 已启动并发 read 组在取消后全部完成或超时。
- [x] G5. 取消后不启动后续工具或下一轮 LLM。
- [x] G6. assistant 工具调用进入历史后，每个 `tool_call_id` 都有真实结果或 `tool_cancelled`。
- [x] G7. 取消路径只产生 `RunCancelledEvent`，不产生 `FinalResponseEvent`。
- [x] G8. TUI 增量显示真实 thinking、正文、工具开始和工具结果。
- [x] G9. Escape 调用 active `AgentRunContext.cancel()`。
- [x] G10. 请求终止后输入框恢复并获得焦点。

## H. 最终命令

- [x] H1. `rg -n "textual" src/mewcode_agent/agent` 无匹配。
- [x] H2. `rg -n "MAX_TOOL_CALLS_PER_TURN|_run_agent_loop|_stream_round" src tests` 无匹配。
- [x] H3. `uv run python -m compileall -q src tests` exit code 为 `0`。
- [x] H4. `uv run pytest -m "not integration"` 为 `0 failed`、`0 errors`。
- [x] H5. `uv run pytest` 为 `0 failed`、`0 errors`。
- [x] H6. `git diff --check` exit code 为 `0`。
- [x] H7. 使用 Task 9 的动态 token 扫描命令检查 `docs/ch02`，没有占位标记。

## 验证证据

| 项目 | 命令或值 | 实际结果 |
| --- | --- | --- |
| 实现 commit | `git rev-parse HEAD` | `3a6b203e05a3efe317b6afb50b6551f9850b44ec` |
| 验收时间 | `Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"` | `2026-07-17T21:03:49+08:00` |
| 编译 | `uv run python -m compileall -q src tests` | exit code `0` |
| 离线测试 | `uv run pytest -m "not integration"` | exit code `0`；`175 passed in 6.19s` |
| 默认测试 | `uv run pytest` | exit code `0`；`175 passed in 7.38s` |
| 补丁格式 | `git diff --check` | exit code `0` |
| 占位扫描 | Task 9 动态 token 扫描命令 | exit code `0`；无匹配 |
| Agent/Textual 边界 | `rg -n "textual" src/mewcode_agent/agent` | 无匹配；rg exit code `1` |
| 旧循环残留 | `rg -n "MAX_TOOL_CALLS_PER_TURN|_run_agent_loop|_stream_round" src tests` | 无匹配；rg exit code `1` |
