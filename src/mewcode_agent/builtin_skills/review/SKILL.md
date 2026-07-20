---
name: review
description: 在隔离上下文中只读审查代码并报告可复现问题
allowed_tools:
  - read_file
  - find_files
  - search_code
  - run_command
execution_mode: isolated
model: inherit
context_strategy: recent
recent_messages: 12
---
# Review SOP

1. 严格按 Skill 参数限定审查范围；参数为空时审查当前工作区尚未提交的修改。
2. 先读取 `git status --short` 和相关 diff，再读取验证问题所需的精确文件与测试。不得从摘要、文件名或相似标识符猜测代码。
3. 全程只读。不得编辑、创建、删除、格式化、暂存或提交文件，也不得运行会改变仓库或外部系统状态的命令。
4. 只报告可以从当前代码和可执行证据复现的问题，重点检查正确性、安全性、数据损坏、并发、错误处理和缺失测试。
5. Findings 按严重程度排序；每项给出精确文件、起始行号、触发条件、影响和最小修复方向。
6. 不把纯风格偏好写成缺陷。如果没有发现问题，明确说明未发现可复现问题，并列出仍未覆盖的测试风险。
7. 最终输出必须是适合直接回流主对话的简洁审查摘要。
