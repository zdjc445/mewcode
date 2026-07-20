---
name: test
description: 在隔离上下文中选择并执行最小充分测试
allowed_tools:
  - read_file
  - find_files
  - search_code
  - run_command
execution_mode: isolated
model: inherit
context_strategy: summary
recent_messages: null
---
# Test SOP

1. 从 Skill 参数、结构化历史摘要、当前 `git status --short` 和相关 diff 确定精确修改范围。
2. 读取项目测试配置和相关测试文件，选择覆盖修改行为的最小充分测试；不得凭技术栈名称猜测命令。
3. 先运行聚焦测试，再按风险决定是否运行更广回归、编译或静态检查。所有命令仍受现有安全策略约束。
4. 本 Skill 只测试和报告，不修改代码、测试、配置或生成物；失败后不得自行实施修复。
5. 逐项记录精确命令、退出码、通过数、失败数、跳过数和关键失败位置。不得隐藏失败或把未运行项目写成通过。
6. 无法运行时说明缺少的精确条件，例如依赖、环境变量、平台能力或外部服务；不得伪造结果。
7. 最终输出必须是适合直接回流主对话的测试摘要，并明确剩余未验证风险。
