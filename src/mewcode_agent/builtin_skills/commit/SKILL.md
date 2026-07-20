---
name: commit
description: 检查当前工作区并创建边界清晰的 Git 提交
allowed_tools:
  - read_file
  - find_files
  - search_code
  - run_command
execution_mode: shared
model: inherit
context_strategy: current
recent_messages: null
---
# Commit SOP

1. 精确读取当前分支、`git status --short`、未暂存 diff、已暂存 diff 和最近提交主题；不得根据文件名猜测修改内容。
2. 区分用户已有修改与本次任务修改。不得丢弃、覆盖、重置、清理或改写用户修改。
3. 按职责和可独立回退性把修改分组。每组提交前运行与该组风险相称的测试或静态检查。
4. 只使用显式路径执行 `git add -- <paths>`，随后复查已暂存 diff，确保没有混入其他组。
5. 使用简洁、具体的提交主题创建普通提交。不得 amend、rebase、reset、force push 或绕过 hooks。
6. 用户明确要求 push 时，提交完成后再次确认分支和远端状态，再执行普通 `git push`；用户没有明确要求时不得 push，也不得创建 PR。
7. 每完成一组就报告提交哈希、主题、包含范围和验证结果；如果没有可提交修改，必须明确说明。
