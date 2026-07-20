# Chapter 09 Evaluation：分层 Skill、按需激活与隔离执行

## 验收环境

| 字段 | 实际值 |
| --- | --- |
| 时间 | `2026-07-21T00:08:07.5943025+08:00` |
| 分支 | `master` |
| 验收代码基线 Commit | `b03c3ef5ac322fb4f4b9216151e0cc7274118e80` |
| 真实 API 条件 | `DEEPSEEK_API_KEY` 未设置 |
| 代码状态 | README、严格嵌套 JSON 校验、隔离错误边界、对应测试与本验收记录尚未提交 |

## 分批提交记录

| Commit | 内容 |
| --- | --- |
| `e071693` | Chapter 09 分层 Skill 规格 |
| `eacf9b2` | frontmatter、目录清单、三层扫描与覆盖 catalog |
| `c5bc3d9` | 目录 Python 工具子进程与 Skill 工具原子注册 |
| `b059f1b` | shared 激活、Prompt 固定控制项与工具可见性 |
| `c92e670` | isolated 执行、三种上下文策略与结果回流 |
| `b03c3ef` | 动态命令、CLI 装配和 commit/review/test 内置样板 |

## 自动化结果

| 检查 | 实际结果 |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -q -rs` | `742 passed, 4 skipped in 17.50s`，exit code `0` |
| Skill loader/executor/commands/CLI 聚焦测试 | `58 passed in 7.20s`，exit code `0` |
| 命令、Skill、App 与 CLI 聚焦回归 | `110 passed in 12.69s`，exit code `0` |
| `.venv\Scripts\python.exe -m compileall -q src tests integration_tests` | exit code `0` |
| `git diff --check` | exit code `0`；只有 Git 的 LF→CRLF 工作区提示 |
| `uv build --wheel` | 成功生成 `mewcode_agent-0.1.0-py3-none-any.whl` |
| wheel 内容检查 | `commit/SKILL.md`、`review/SKILL.md`、`test/SKILL.md` 均存在于 `mewcode_agent/builtin_skills/` |

四个跳过项分别是：

- `tests/test_instruction_loader.py::test_symlink_outside_root_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_notes_storage.py::test_project_notes_symlink_escape_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_security_boundary.py::test_path_sandbox_rejects_symlink_escape`：当前 Windows 环境不允许测试创建目录符号链接；
- `tests/test_session_storage.py::test_session_permissions_are_private_on_posix`：只适用于 POSIX 权限契约。

## 已验证场景

- 项目 `.mewcode/skills`、用户 `.mewcode-agent/skills` 和包内 builtins 按 project > user > builtin 完整覆盖；高层无效候选产生脱敏诊断后允许低层同名候选生效；
- 直接 `<name>.md` 与目录 `<name>/SKILL.md` 都可加载，只扫描根目录直接子项，不根据文件或目录名推断规范名称；
- YAML frontmatter 起止边界、重复键、未知/缺失字段、类型、组合约束、空正文、LF/CRLF 和 UTF-8 均严格校验；
- Skill `name` 精确匹配 `[a-z][a-z0-9-]*`；目录工具 `name` 精确匹配 `[a-z][a-z0-9_]{0,63}`，不转换大小写或字符；
- 同一来源的有效同名候选全部淘汰，不按扫描顺序任选；项目、用户和内置层的扫描与诊断顺序稳定；
- 最终 catalog 对白名单缺失、专属工具全局冲突、`mcp_`/`load_skill` 保留名称和固定命令冲突执行启动 fail-fast；被覆盖候选的工具不参与注册或冲突判断；
- `tools.yaml` 精确校验 `version`、工具字段、`category: command`、`timeout_seconds`、相对 POSIX `.py` 路径与 Draft 2020-12 JSON Schema；嵌套非字符串 object key、非 JSON 值和非有限浮点数被拒绝；
- 目录脚本通过 `sys.executable` 与绝对脚本路径启动，`shell=False`，cwd 固定为 Skill 目录，参数通过 UTF-8 JSON stdin 传入，stdout 只接受一个完整 JSON value；
- 参数 schema 失败时脚本不会启动；非零退出、超时、非 UTF-8、无效 JSON 与 `NaN` 返回稳定脱敏错误，stderr 不进入工具结果；取消超时会终止并等待子进程；
- ToolRegistry 能原子替换全部 Skill 工具，同时保留核心工具和所有 MCP server 工具；Provider schema 可以按精确 `frozenset` 过滤；
- 启动环境只注入按名称排序的 `name` 与 `description` 目录，不泄露 SOP、白名单、路径、模式或工具 schema；
- `load_skill` 使用精确 `name` 和逐字符保留的 `arguments`；每次加载重新读取源文档和清单，名称变化返回 `skill_source_changed` 并要求 rescan；
- shared Skill 的完整 SOP 和参数进入可原子替换的 session context control，不进入 `ConversationHistory` 或 JSONL；重复激活更新内容但保持首次顺序；
- 无 active Skill 时专属脚本工具不出现在 Provider schema；存在一个 shared Skill 时按其白名单过滤；多个 shared Skill 同时激活时取交集并始终加入 `load_skill`；
- ToolScheduler 在执行前再次检查同一可见集合，隐藏工具即使由 Provider 返回名称也产生 `tool_not_visible`，不会执行；
- Prompt 动态 session controls 可以在活动 request 中替换并保持 sequence、anchor 有效；session reset 清除动态 controls，Skill runtime 随后只恢复当前目录；
- isolated Skill 使用独立 `ConversationHistory`、PromptRuntime、active 集合和 AgentRunContext，同时复用 Provider、registry、scheduler 和安全策略；
- isolated `none` 不携带主历史；`recent` 从尾部选择并向前扩展到完整工具事务；损坏的工具历史返回稳定 `skill_isolated_failed`；
- isolated `summary` 使用现有 `tools=None` 摘要器生成结构化 checkpoint，并附加上下文边界；用户原始消息由代码逐字符写入 checkpoint；
- isolated 内部 user/assistant/tool 历史不修改主历史，只把最终响应放进外层 `load_skill` 结果；失败、取消和无最终响应不回流部分历史；
- nested `load_skill` 使用 ContextVar 绑定到对应隔离 runtime，不会把嵌套 shared 激活写入主会话 active 集合；
- 隔离 run 遇到新的 `ToolApprovalRequestedEvent` 固定选择 `reject`，避免无法转发到外层 TUI 的确认死锁；策略、session 或永久规则直接允许的调用正常执行；
- CommandRegistry 支持冻结后的动态命令目录原子替换；冲突验证失败保持原目录不变，帮助、状态和 Tab 补全读取同一 registry 对象；
- `/skills` 显示名称、说明、来源、模式与 active 状态；`/skills show <name>` 显示脱敏元数据但不显示 SOP；`/skills rescan` 原子替换 catalog、专属工具、动态命令和 active controls；
- 每个生效 Skill 自动生成 `/<name> [arguments]`，参数原文进入固定合成请求，固定使用 execute 但不修改 UI 默认模式；
- Chapter 08 的固定 `/review` 和 `/code-review` 已移除；内置 `review` Skill 提供动态 `/review`，`/code-review` 按未知命令处理；
- CLI 在 MCP 激活后扫描 Skill，确保 MCP 名称可以参与白名单校验；随后注册 `load_skill`、构造共享/隔离 runtime、注入工具可见性并注册动态命令；
- `/clear`、`/resume` 与新 session activation 在 Prompt timeline reset 后清空 active Skill 并重新注入当前目录；历史存档不保存或恢复 active SOP；
- wheel 中包含 shared `commit`、isolated recent `review` 和 isolated summary `test` 三个内置样板；用户或项目同名 Skill 可以完整覆盖；
- Chapter 01–08 默认测试继续通过，会话仍没有任何自动清理路径。

## 未连接的外部系统

本次默认验收没有调用 DeepSeek API，因为 `DEEPSEEK_API_KEY` 未设置。shared/isolated Agent、摘要、嵌套加载、工具调用、审批拒绝、命令热更新与错误流均使用本地确定性测试替身验证。

测试没有连接用户 MCP server，没有执行用户主目录中的真实 Skill 脚本，也没有读取或修改用户主目录中的真实 Skill、会话、笔记或安全配置。`uv build --wheel` 只构建本地发布产物并检查包内容，没有发布到外部 registry。
