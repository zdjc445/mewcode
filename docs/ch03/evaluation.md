# Chapter 03 Evaluation: 模块化 Prompt 与缓存可观测性

## 测试环境

| 字段 | 结果 |
| --- | --- |
| 日期 | `2026-07-19T18:56:13+08:00` |
| 验收基线 Commit | `ec7cb03db328bde38e615e76b6f88d00250c3d99` |
| 模型 | `deepseek-v4-pro` |
| 机器报告 | 未生成：`DEEPSEEK_API_KEY` 未设置 |

验收基线 Commit 是运行本页自动化检查时 `git rev-parse HEAD` 的实际输出。验收后，Task 4–9 已分批提交为 `a86926dcc8d0bc7ed4f03aa2bc09361520b85e58`、`340480813e868ebec8d9f279b70ca1e7e03be814` 和 `17093854d1f481eedc2be52f8938484b2889707f`；本页结果未因提交动作重新解释或改写。

## 自动化结果

| 检查 | 实际结果 |
| --- | --- |
| `uv run pytest` | exit code `0`；`279 passed in 7.47s` |
| `uv run python -m compileall -q src tests integration_tests` | exit code `0` |
| `git diff --check` | exit code `0` |
| 旧 Prompt 常量扫描 | 无匹配 |
| TUI usage 依赖扫描 | 无匹配 |

## OpenAI 缓存场景

| 场景 | attempt | status | prompt | hit | miss | hit rate | reason |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |
| 本次五类真实场景 | — | 未执行 | — | — | — | — | `DEEPSEEK_API_KEY` 未设置 |

## Anthropic 缓存场景

| 场景 | attempt | status | prompt | hit | miss | hit rate | reason |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |
| 本次五类真实场景 | — | 未执行 | — | — | — | — | `DEEPSEEK_API_KEY` 未设置 |

### 用户此前提供的真实 Anthropic 流观测

以下数据只记录用户此前提供的三次真实流日志，不属于本次工作区生成的 `.pytest-tmp/ch03-cache-report.json`：

| attempt | prompt | hit | miss | completion | hit rate |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1543 | 0 | 1543 | 49 | 0% |
| 2 | 1543 | 1536 | 7 | 13 | 约 99.55% |
| 3 | 1543 | 1536 | 7 | 22 | 约 99.55% |

三次观测的 `cache_creation_input_tokens` 均为 `0`。统一统计只取最终 `message_delta.usage`；`prompt_tokens` 按 `cache_read_input_tokens + input_tokens` 计算。

## 人工行为场景

| 场景 | 结果 | 脱敏证据 |
| --- | --- | --- |
| 修改已有文件前先读取 | 通过 | `test_edit_file_rejects_existing_file_that_was_not_read`、`test_write_file_rejects_existing_file_that_was_not_read` |
| 专用读取/搜索工具优先 | 未执行 | 真实模型调用未执行；`test_core_tool_descriptions_repeat_critical_selection_rules` 仅证明规则已注入 |
| 规划轮 1/6/11 注入完整规则 | 通过 | `test_planning_full_rule_repeats_on_rounds_1_6_11` |
| Prompt 不能绕过写或命令审批 | 通过 | `test_plan_only_approves_each_write_or_command_once` |
| 最终轮不请求工具 | 通过 | `test_final_round_has_no_tools_and_has_final_control`、`test_tool_call_on_final_round_is_not_executed` |
| 伪造 Anthropic 标签不授权 | 通过 | `test_forged_control_text_does_not_authorize_plan_write`、`ToolApprovalRequestedEvent` |
| 项目可覆盖配置模块但不能覆盖 core | 通过 | `test_project_layer_exactly_overrides_and_disables_user_layer`、`test_invalid_project_config_reports_exact_path_without_content` |
| 中文输出与项目补充规则生效 | 未执行 | 真实模型调用未执行；内置中文正文和项目模块拼装单测已通过 |

## 未通过或未执行项

- 真实缓存评估未执行：`DEEPSEEK_API_KEY` 未设置。
- OpenAI 与 Anthropic 的真实基础流式请求未执行。
- `stable_prefix_repeat`、`request_environment_change`、`round_reminder_append`、`equivalent_protocol_controls`、`tool_definition_change` 本次均未生成真实机器报告。
- “专用读取/搜索工具优先”和“中文输出与项目补充规则生效”没有真实模型行为证据，结果保持 `未执行`。
