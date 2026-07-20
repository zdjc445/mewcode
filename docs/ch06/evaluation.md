# Chapter 06 Evaluation：两级上下文压缩与工具结果外置

## 验收环境

| 字段 | 实际值 |
| --- | --- |
| 时间 | `2026-07-20T11:17:15.9218424+08:00` |
| 分支 | `master` |
| 验收代码基线 Commit | `3c7ac198ba100d3b547707612f89fd7f089013a2` |
| 模型配置 | `deepseek-v4-pro`，`context_window_tokens: 1000000` |
| 真实 API 条件 | `DEEPSEEK_API_KEY` 未设置 |
| 代码状态 | 最终验收测试与文档改动尚未提交 |

## 自动化结果

| 检查 | 实际结果 |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -q -rs` | `521 passed, 1 skipped in 14.76s`，exit code `0` |
| Chapter 06、Agent、CLI 与双 Provider 聚焦测试 | `168 passed in 10.52s`，exit code `0` |
| `.venv\Scripts\python.exe -m compileall -q src tests integration_tests` | exit code `0` |
| `git diff --check` | exit code `0`；只有 Git 的 LF→CRLF 工作区提示 |

唯一跳过项是 `tests/test_security_boundary.py::test_path_sandbox_rejects_symlink_escape`：当前 Windows 环境不允许测试创建目录符号链接。Chapter 06 新增测试没有跳过项。

## 已验证场景

- 当前两个 Provider 配置都严格要求 `context_window_tokens: 1000000`，并验证该值大于 `max_tokens`；
- 单个结果 `64 KiB`、批次 `128 KiB`、预览正文 `8 KiB` 的 UTF-8 字节边界和等值不外置规则通过测试；
- 批次超限时按原始大小降序、历史索引升序选择，预览仍超限时降为 metadata-only；
- artifact 使用 SHA-256 文件名和原子写入，同内容复用，单文件 `64 MiB`、单 session `512 MiB` 上限生效；
- `read_context_artifact` 只接受当前 session 已登记的精确绝对路径，并在每次分页读取前重新验证路径、普通文件、长度和 SHA-256；
- 正常 CLI 退出删除当前 session 目录；陈旧目录清理只处理超过 `24` 小时且名称严格匹配 32 位小写十六进制的目录；
- Layer 1 在 Prompt 组装和 Layer 2 压力判断前执行，历史替换保持消息数量、`tool_call_id` 和控制锚点不变；
- 完整与增量估值都基于 Provider 的确定性 payload；可用的真实 `prompt_tokens` 成为下一次普通请求的测量基线；
- 自动触发值为 Prompt 预算的 `80%`，摘要候选目标为 `60%`，同一普通 request sequence 最多尝试一次自动摘要；
- 摘要请求使用当前 Provider，`tools` 精确为 `None`，System Prompt 首尾均禁止工具；任何 `ProviderToolCall` 都被拒绝且不会进入 scheduler；
- 摘要流的事件顺序、usage、stop reason、`64 KiB` 正文上限、JSON 根字段顺序和八个模型生成部分均经过严格校验；
- `analysis_draft` 解析后丢弃，`user_messages_verbatim` 只由代码从原始历史逐字符生成；
- checkpoint 只覆盖完整原子前缀，工具调用和全部结果不会被拆开；失败或取消保留旧 checkpoint；
- OpenAI 将 summary 和 boundary 转为连续 system 消息，Anthropic 将它们转为合法 user text blocks；
- 普通历史和旧 `ControlMessage.anchor` 不删除、不重排；投影保留全部原始 user 消息、session control 和当前活动 request/round control；
- 自动摘要成功、失败 warning 和熔断分别通过结构化事件进入 TUI，事件不包含摘要正文、用户原话或 artifact 内容；
- 连续失败 `3` 次后自动熔断，同一 session 只发一次熔断 warning；精确 `/compact` 可绕过熔断，成功后重置；
- `/compact` 不进入历史且不递增 Prompt request sequence；大小写不同或携带其他文本时按普通用户消息处理；Escape 会取消手动或自动摘要并保持事务边界；
- 摘要 usage 记录类型为 `CompactionUsageRecord`，`request_kind` 精确为 `compaction`；普通轮次仍为 `agent`；
- 端到端测试真实执行超过 `64 KiB` 的工具结果，下一轮只收到外置预览，再通过 `read_context_artifact` 恢复原始紧凑 JSON 并完成 Agent run；
- Chapter 01–05 默认测试继续通过。

## 未连接的外部系统

本次默认验收没有调用 DeepSeek API，因为 `DEEPSEEK_API_KEY` 未设置；因此没有生成真实百万 Token 压力会话或真实摘要质量样本。Provider stream、usage、工具调用、超时、取消和错误路径全部使用本地确定性测试替身验证。测试没有连接用户 MCP server，也没有读取用户已有的 context artifact。
