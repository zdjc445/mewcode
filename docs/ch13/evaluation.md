# Chapter 13 Evaluation：持久 Team 协作、DAG 调度与安全集成

## 验收环境

| 字段 | 实际值 |
| --- | --- |
| 时间 | `2026-07-21T04:10:03.4021479+08:00` |
| 分支 | `master` |
| 验收代码基线 Commit | `11a4fc4` |
| 真实 API 条件 | `DEEPSEEK_API_KEY` 未设置 |
| 代码状态 | 本验收记录尚未提交；其余 Chapter 13 代码、测试、README 与入口配置已提交并推送 |

## 分批提交记录

| Commit | 内容 |
| --- | --- |
| `30e9aed` | Chapter 13 持久 Team、共享任务 DAG 与集成编排规格 |
| `ee92267` | 严格配置/模型、项目 Git 级状态、mailbox/history 与原子持久化基础层 |
| `b9deda4` | 可替换 backend protocol 与复用现有 Worker 的 in-process backend |
| `90d680e` | 持久 DAG、确定性 Lead 调度、通知、恢复与关闭生命周期 |
| `21a9929` | completed task 到 integration、经确认 integration 到 main 的安全 Git 集成流程 |
| `abeced8` | Provider 工具、本地命令、CLI 装配、请求通知与 runtime 关闭顺序 |
| `11a4fc4` | mailbox 崩溃尾行恢复与 Team 不可用时禁止未知 worktree 自动清理 |

## 自动化结果

| 检查 | 实际结果 |
| --- | --- |
| `.venv\\Scripts\\python.exe -m pytest -q` | `1031 passed, 5 skipped in 44.64s`，exit code `0` |
| Team 最终审计修正聚焦回归 | `97 passed in 27.30s`，exit code `0`；随后由最终全量回归再次覆盖 |
| `.venv\\Scripts\\python.exe -m compileall -q src tests integration_tests` | exit code `0` |
| `git diff --check` | exit code `0` |
| `uv build --wheel` | 成功生成 `dist/mewcode_agent-0.1.0-py3-none-any.whl` |
| wheel 内容检查 | `mewcode_agent/teams/` 的 8 个模块全部存在；console entry point 为 `mewcode_agent.cli:console_main` |

五个跳过项为：

- `tests/test_instruction_loader.py::test_symlink_outside_root_is_rejected`：当前环境不允许创建符号链接；
- `tests/test_notes_storage.py::test_project_notes_symlink_escape_is_rejected`：当前环境不允许创建符号链接；
- `tests/test_security_boundary.py::test_path_sandbox_rejects_symlink_escape`：当前环境不允许创建目录符号链接；
- `tests/test_session_storage.py::test_session_permissions_are_private_on_posix`：只适用于 POSIX 权限契约；
- `tests/test_worktree_initializer.py::test_source_symlink_escape_is_refused`：当前环境不允许创建文件符号链接。

## 已验证场景

### 配置、身份、状态与持久化

- `teams.yaml` 不存在时只使用内存默认值；存在时严格拒绝重复 key、未知/缺失字段、bool 伪整数、越界数值和无效 backend；
- Team/Member/Task/Message 名称、ID、枚举、时间、路径与关联关系使用严格模型，不 lower、不修复、不靠目录或文本猜测恢复；
- Team 状态写入使用同目录临时文件、flush、fsync 与 replace；跨进程 lock 沿用 live PID、300 秒过期和无法确认时 fail-closed 的契约；
- mailbox/history 使用 append-only UTF-8 JSONL、单行上限、flush 和 fsync；mailbox 中间坏行拒绝，最终解析失败行按崩溃残留忽略；文件不截断、不压缩、不自动删除；
- member history 只恢复最后配置数量的完整 user/assistant 对；mailbox cursor 在 episode 终态和 history 持久化后推进，崩溃窗口允许重复投递而不静默丢消息。

### Team、DAG、调度、通知与恢复

- 一个 runtime 最多一个 active/paused Team；创建时严格验证成员角色存在且声明 `isolation: worktree`，并创建独立 manual integration worktree；
- task 创建拒绝不存在、自依赖、重复依赖和环；只有 dependencies 为 completed/integrated 的 task ready，任务和 idle member 均按稳定顺序确定性分配；
- 每个 member 同时最多一个 task、每个 task 最多一个 owner；pause 允许运行 episode 收尾但不再启动，resume 唤醒 scheduler，cancel/close 会 cancel、gather 并持久化稳定终态；
- in-process backend 复用 WorkerCatalog、WorkerManager 与 WorkerExecutor；Team task ID 直接成为 Worker task ID，并保留 worktree workspace snapshot；
- 运行中的持久 task 在重启后固定转为 `failed/team_member_interrupted`，member 转 idle 并向 lead mailbox 写通知，不猜测或接管旧进程；
- task terminal/integrate 通知进入持久 lead mailbox；下一次主请求按批次形成 request controls 并持久推进 cursor，不写普通 ConversationHistory、session 摘要或 notes。

### 权限、Worker 与 Worktree 边界

- Team 管理工具只注册到主 Agent；所有 Worker 的最终可见集合都强制移除 `spawn_worker` 和全部 `team_*` 工具，Skill catalog 引用这些保留名会 fail-fast；
- member instructions、mailbox 和历史只作为不可信 Prompt 数据，不改变角色、工具白名单、Hook、权限或 PathSandbox；in-process member 沿用无 UI 审批边界；
- Team task worktree 使用 Chapter 12 的 ContextVar working directory/root 隔离；`worker/<32hex>` 接受数字开头的内部 task ID，其他名称契约不放宽；
- WorkerExecutor 对 Team workspace 使用 `preserve_workspace`，不会按普通 background Worker 生命周期删除；WorktreeManager 启动清理前先注册所有持久 Team worker/integration worktree 为 protected；
- Team 状态损坏、仓库不可用或 manager 未成功恢复时不启动 worktree 自动清理，避免在 ownership 未知时删除 Team 数据；closed/merged Team 的文件、mailbox、history、branch 与 worktree 均不自动清理。

### Git 集成、工具、命令与 CLI

- `team_integrate` 重新验证 task completed、workspace 名称/branch/HEAD、task 与 integration 实时 clean；只在 integration worktree 执行 `--no-ff --no-edit` merge；冲突尝试 abort 并保留全部数据；
- `/team merge --into-main` 先验证任务完整和两边状态，显示 main/integration path、HEAD、task counts 与 dirty 摘要并要求 destructive confirmation；确认后重新检查；
- main 合并先让 integration 吸收当前 main，再重新确认 main HEAD 未变化，最后只允许 `--ff-only` 更新主工作树；失败不 reset、不 clean、不删除 worktree；
- 临时真实 Git 仓库测试覆盖 task branch 到 integration 和 integration 到 main 的成功路径，以及 dirty、错误 branch/HEAD、冲突、未完成任务、main 变化与拒绝确认路径；
- `team_create`、`team_task`、`team_message`、`team_status`、`team_integrate` 使用固定严格 schema；`/teams`、`/team show|pause|resume|close|merge` 全部本地分发，close/merge 要求确认；
- CLI 加载配置、恢复状态、注册工具/命令、组合 Worker/Team request controls，并按 Team→Worker→Worktree→其他 runtime 的顺序关闭；非 Git 或 Team 状态不可用时普通 Agent 仍可启动，Team 入口返回稳定错误。

## 未连接或未执行的外部场景

本次默认验收没有调用 DeepSeek API，因为 `DEEPSEEK_API_KEY` 未设置；AgentLoop、Provider 与成员 backend 行为使用本地确定性替身。没有连接公网、真实远端 Git remote、真实 MCP server 或插件。

Git 集成测试只在 pytest 临时目录的真实本地仓库中创建 branch/worktree 和执行 merge。实现与测试都不会自动 commit、push、创建 PR、reset、clean、删除远端 branch，或自动删除 Team 状态、mailbox、history、task/integration worktree。`uv build --wheel` 只构建本地产物，没有发布到外部 registry。

当前唯一实现的是完整可用的同进程 `in_process` backend。tmux、Windows Terminal、PTY 或其他跨进程 pane backend 未实现，也不会返回伪成功；仓库当前没有无 UI Agent 子进程入口，仍需先定义进程认证、secret 传递、心跳、终端复用和崩溃接管协议。现有 backend protocol 已把调度、状态、邮箱和 Worker 细节隔离，后续实现无需改变持久 schema。

本次没有人工操作真实 Textual 终端完成 Team 命令流程；命令解析、确认、UI 消息、request controls、资源关闭顺序和 CLI unavailable 路径由确定性测试验证。POSIX 权限和当前 Windows 主机不允许创建的 symlink 路径未执行，对应跳过原因如上。
