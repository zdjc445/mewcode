# Chapter 11 Evaluation：统一子工作者、Fork 与后台任务

## 验收环境

| 字段 | 实际值 |
| --- | --- |
| 时间 | `2026-07-21T02:10:43.0751843+08:00` |
| 分支 | `master` |
| 验收代码基线 Commit | `2626c9c7e06ed2c979d354a1da19f11217ddfee9` |
| 真实 API 条件 | `DEEPSEEK_API_KEY` 未设置 |
| 代码状态 | 本验收记录尚未提交，其余 Chapter 11 代码、测试与 README 已提交并推送 |

## 分批提交记录

| Commit | 内容 |
| --- | --- |
| `3e65bf9` | Chapter 11 统一子工作者、Fork 与后台任务规格 |
| `e362de0` | 四层角色扫描、严格 frontmatter、运行配置和四个内置角色 |
| `7ab9cdd` | ContextVar 文件状态与 Hook Prompt 路由、当前 session controls 复制 |
| `3a16627` | 独立 AgentLoop 执行、Token 统计、Fork 历史、工具过滤和前后台任务管理 |
| `2626c9c` | CLI、单一工具、命令、通知、ESC、Skill 隔离与 Hook subagent 接线 |

## 自动化结果

| 检查 | 实际结果 |
| --- | --- |
| `uv run pytest -q` | `857 passed, 4 skipped in 26.68s`，exit code `0` |
| Worker/Agent/App/CLI/Hook/Skill 聚焦测试 | `171 passed in 21.96s`，exit code `0` |
| `python -m compileall -q src tests integration_tests` | exit code `0` |
| `git diff --check` | exit code `0`；只有 Git 的 LF→CRLF 工作区提示 |
| `uv build --wheel` | 成功生成 `mewcode_agent-0.1.0-py3-none-any.whl` |
| wheel 内容检查 | `explore.md`、`plan.md`、`general.md`、`verify.md` 均存在于 `mewcode_agent/builtin_workers/` |

四个跳过项分别是：

- `tests/test_instruction_loader.py::test_symlink_outside_root_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_notes_storage.py::test_project_notes_symlink_escape_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_security_boundary.py::test_path_sandbox_rejects_symlink_escape`：当前 Windows 环境不允许测试创建目录符号链接；
- `tests/test_session_storage.py::test_session_permissions_are_private_on_posix`：只适用于 POSIX 权限契约。

## 已验证场景

- 项目 `.mewcode/workers/`、用户 `~/.mewcode-agent/workers/`、包内 builtin 和显式 plugin roots 按 project > user > builtin > plugin 完整覆盖；只扫描直接 `.md` 子文件；
- 无效候选产生不含 SOP 的脱敏诊断并允许低层同名定义回退；同来源有效重名全部失效；多个插件同名不会按注册顺序任选；
- Worker frontmatter 对边界、重复键、未知/缺失字段、名称、白黑名单、Provider、轮数、权限模式、隔离模式、正文、LF/CRLF 和 UTF-8 严格校验；
- 用户 `workers.yaml` 不存在时只使用内存默认值且不创建文件；存在时严格校验 `version`、并发、前台 timeout、后台白名单与 verify 开关；
- 最终角色和后台白名单引用不存在工具、角色引用不存在 Provider 时启动 fail-fast；`spawn_worker` 作为保留名称参与拒绝和引用校验；
- 默认生效内置角色为 `explore`、`plan`、`general`；内置 `verify` 只在开关启用时生效，用户或项目同名 `verify` 不受隐藏开关影响；
- Provider 工具入口固定为 `spawn_worker`，schema 只接受 `task`、`type`、`background`；不接受大小写修正、角色别名或未知字段；
- 精确 `type` 启动定义式 Worker；省略 `type` 进入 Fork 并强制后台；Chapter 12 前 `isolation: worktree` 返回 `worker_isolation_unavailable`；
- 定义式 Worker 使用空历史、完整角色 SOP session control、角色 Provider 和 max rounds；Fork 使用父历史的最后完整工具事务边界与父可见工具快照；
- Fork 不复制当前 `spawn_worker` call 或同批不完整结果；损坏的更早工具事务不会被静默修复；首条任务 Prompt 包含不可嵌套、不可提问、直接使用工具和四段限长报告约束；
- 工具集合依次执行基础集合、移除 `spawn_worker`、角色 allowed、角色 denied 和后台白名单收窄；隐藏工具在 Provider schema 与 ToolScheduler 两层都不可执行；
- 每个 Worker 使用独立 ConversationHistory、AgentRunContext、SecurityPolicyEngine session rules、FileStateCache ContextVar 和 UsageCollector；主 Agent 与其他 Worker 不共享“已读取”状态或临时审批；
- Worker 遇到审批请求固定拒绝，不打开主 UI、不写 session/permanent allow；已由策略直接允许的调用继续执行；
- available usage 精确累计 prompt、cache hit、cache miss 和 completion token；unavailable/invalid 只累计轮数，不猜测 Token；
- PromptRuntime 复制当前静态和动态 session controls；active shared Skill 在 Worker 中形成独立副本，Worker 内后续 Skill 激活不修改主会话；
- Worker 使用同一 HookEngine；Prompt Hook 通过 ContextVar 只注入对应 Worker runtime，退出时丢弃未消费 pending，不进入主会话；
- Hook `subagent` 已真实接入后台 Worker；`none` 使用 general 空历史，`recent` 携带最近 12 条并扩展到原子边界，`summary` 使用 tools=None 的结构化摘要和边界；Worker 内再次触发会被代码层拒绝；
- WorkerManager 强制并发上限和单一前台等待方；显式后台、Fork 强制后台、前台 timeout 与 ESC 四条路径均保留同一 asyncio task，不重启 AgentLoop；
- 外层取消通过 shield 不会连带取消 Worker；ESC 先将活动前台任务标记 `escape`，再取消父 AgentRunContext；普通等待超时标记 `timeout`；
- 任务记录包含 ID、session、type、kind、状态、模式、transition、Provider/model、可见工具、时间、usage、完整结果、错误码与报告格式状态；异常正文和 traceback 不进入记录；
- 只有进入后台的终态任务排队通知；结果通知硬限制为最多 8000 code points，只在相同 session 的下一主请求中作为 request context control 注入，不写 ConversationHistory 或 session JSONL；
- 会话切换清除旧 session 未消费通知；任务记录仍可由命令查询；同一通知最多消费一次；
- `/workers`、`/workers roles`、`/worker show <task_id>` 和经确认的 `/worker cancel <task_id>` 均本地执行，不进入普通历史；task ID 和子命令保持精确匹配；
- WorkerManager close 停止接单、请求 AgentRunContext 取消、强制取消残余 task、gather、确定终态、清空通知并返回幂等统计；CLI 在 UI 退出后先关闭 Worker，再刷新笔记和关闭 Hook；
- CLI 在 MCP 与 Skill 工具注册后扫描 Worker，缓存每个 Provider adapter，构造独立安全与压缩管理器，注册 `spawn_worker`、命令、通知和 Hook subagent；
- README 已记录角色来源、运行配置、前后台语义、命令、状态隔离和共享文件系统边界；Chapter 01–10 默认测试继续通过，且没有新增自动清理用户文件的路径。

## 未连接的外部系统

本次默认验收没有调用 DeepSeek API，因为 `DEEPSEEK_API_KEY` 未设置。定义式/Fork AgentLoop、Provider 请求、工具审批、后台状态机、通知、Hook subagent 和 Token 统计均使用本地确定性 Provider、Tool、ContextSummarizer 与 UI 替身验证。

测试没有启动用户主目录中的真实 Worker、Hook、MCP server 或插件角色，也没有访问公网。`uv build --wheel` 只构建本地产物并检查包内容，没有发布到外部 registry。Git worktree 隔离按 Chapter 11 边界保持显式不可用，将在 Chapter 12 实现。
