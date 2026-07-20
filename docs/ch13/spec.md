# Chapter 13 Specification：持久团队、共享任务 DAG 与集成编排

## 1. 目标与边界

本章把 Chapter 11 的一次性 Worker 提升为项目级长期团队：成员身份、共享任务、邮箱和结果在应用重启后仍可恢复；Lead 由主 Agent 担任，后台调度器把依赖已满足的任务交给空闲成员；Chapter 12 的 task worktree 分支按显式步骤合并到独立 integration worktree，并只在用户确认后 fast-forward 到主 worktree。

本章完成：

1. 严格用户配置、项目 Git 级团队状态、跨进程锁与原子持久化；
2. 持久 Team/Member identity、append-only mailbox、成员短期历史和恢复；
3. 共享任务 DAG、依赖状态、精确 assignee、取消和终态；
4. Lead 自动调度、成员串行 ownership、并发容量和退出收尾；
5. 基于现有 WorkerExecutor/WorkerManager 的 in-process backend；
6. Provider 工具、主请求通知、`/teams` 与 `/team` 管理命令；
7. completed task branch 到 integration branch 的显式集成，以及经用户确认的 integration 到 main 快进；
8. backend protocol，使后续跨进程实现不改变状态、任务或邮箱 schema。

本章不完成：

- tmux、Windows Terminal、PTY 或其他跨进程 pane backend；
- 跨主机、数据库、云同步或多人同时编辑同一 common Git dir；
- 自动解决 merge conflict、自动修改成员产出的 dirty 文件；
- 自动 commit、push、PR、远端 branch 删除或主分支强制更新；
- 团队市场、角色下载或版本管理；
- 自动清理 closed team、mailbox、history、task worktree 或 integration worktree。

Chapter 11 曾把“多后端/跨进程 pane”整体列为 Chapter 13 后续项。本章把状态无关的 backend protocol 与 in-process 实现落地，但不把没有 headless Agent 入口的终端窗格伪装成可工作的 backend。pane backend 需在后续章节定义进程认证、secret 传递、心跳、终端复用和崩溃接管协议。

## 2. 核心不变量

1. 一个应用 runtime 同时最多激活一个 team；同一 common Git dir 可保存多个 inactive/closed team。
2. Team、Member、Task 和 Message ID 由代码生成，用户不能指定或修复。
3. Lead 是主 Agent；团队成员不能创建 Team、添加成员、修改 DAG 或合并到 main。
4. Member identity 长期存在，但每个 task 仍使用一次 Worker Agent run；跨 task 只携带持久 mailbox 和精简的 user/result 历史，不复制旧工具事务。
5. 每个成员同时最多运行一个 task；每个 task 同时最多有一个 owner；同一 task 不复制执行。
6. 只有全部 dependencies 达到 `completed` 或 `integrated` 的 pending task 才 ready。
7. 团队成员必须引用最终 catalog 中 `isolation: worktree` 的定义式角色；Fork 和 `isolation: none` 不能成为团队成员。
8. Team 调度不会扩大角色、后台白名单、安全策略或工具权限；成员仍无交互审批通道。
9. 邮箱是 append-only JSONL；状态 cursor 只在对应 task episode 成功或终态保存后前移，允许崩溃后重复投递，不允许静默丢消息。
10. 自动调度不自动 commit、merge、push 或删除 worktree。
11. task 集成只接受 clean、branch 匹配且 HEAD 是提交 OID 的 completed worktree；dirty/status error 一律拒绝。
12. integration 永远先吸收 task branch；main 只在本地 UI 破坏性确认后更新，并且只允许 `--ff-only`。
13. merge conflict 只在 integration worktree 出现；失败立即尝试 `git merge --abort`，不在 main 上自动解决。
14. 状态、mailbox、通知和错误不保存 API key、thinking、Provider raw response、工具正文、Git stderr 或 traceback。
15. closed/merged team、mailbox、history 和 worktree 不自动清理。

## 3. 模块结构

```text
src/mewcode_agent/teams/
├── __init__.py
├── models.py       # config/team/member/task/message/result/error
├── loader.py       # ~/.mewcode-agent/teams.yaml 严格配置
├── storage.py      # state JSON、mailbox/history JSONL、lock 与 atomic replace
├── backend.py      # backend protocol 与 in-process Worker adapter
├── manager.py      # DAG、调度、恢复、通知、集成与关闭
├── tools.py        # team_create/team_task/team_message/team_status/team_integrate
└── commands.py     # /teams 与 /team
```

现有模块改动：

| 模块 | 改动 |
| --- | --- |
| `workers.manager` | 增加等待任意 background task 终态但不改变 mode 的 `wait_terminal` |
| `workers.executor` | 现有 task worktree、workspace snapshot 和持久历史入口直接复用 |
| `tools.registry` | 注册固定 Team 工具；工具名加入保留集合 |
| `agent.loop` | request control provider 同时注入 Worker 与 Team 未读通知 |
| `app` | 持有 TeamManager 供关闭和状态显示，不增加成员流式 UI |
| `cli` | 加载配置/状态、构造 backend/manager/tools/commands，关闭顺序 teams→workers→worktrees→其他 |

## 4. 用户配置

只读取：

```text
~/.mewcode-agent/teams.yaml
```

文件不存在时使用内存默认值且不创建：

```yaml
version: 1
max_teams: 8
max_members_per_team: 8
max_tasks_per_team: 256
scheduler_interval_seconds: 1
member_timeout_seconds: 900
member_history_messages: 40
```

约束：

- `version` 是整数 `1`；
- `max_teams` 是 `1..32` 整数；
- `max_members_per_team` 是 `1..16` 整数；
- `max_tasks_per_team` 是 `1..4096` 整数；
- `scheduler_interval_seconds` 是 `1..60` 整数；
- `member_timeout_seconds` 是 `30..86400` 整数；
- `member_history_messages` 是 `2..200` 的偶数；
- 重复键、bool 伪整数、未知/缺失字段或非 UTF-8 使启动 fail-fast；
- 配置变更需重启，不由项目配置覆盖。

## 5. 身份与状态

### 5.1 名称与 ID

Team name 与 Member name 完整匹配：

```regex
[a-z][a-z0-9_-]{0,31}
```

保留 member name：`lead`、`integration`、`system`。名称不转换大小写。

ID：

- `team_id`：`t` 加 31 位小写 hex，总长 32；
- `member_id`、`task_id`、`message_id`：32 位小写 hex；
- 用户输入不能提供 ID；测试可注入 id factory；
- 同一状态内 name 与 ID 分别唯一。

### 5.2 状态路径

```text
state:     <common_git_dir>/mewcode-agent/teams.json
lock:      <common_git_dir>/mewcode-agent/teams.lock
mailbox:   <common_git_dir>/mewcode-agent/teams/<team_id>/mailboxes/<recipient>.jsonl
history:   <common_git_dir>/mewcode-agent/teams/<team_id>/histories/<member_id>.jsonl
```

state 顶层固定：

```json
{
  "version": 1,
  "main_root": "<absolute>",
  "active_team_id": null,
  "teams": []
}
```

Team record 固定包含：

```text
team_id, name, state(active|paused|closed|merged),
base_head, integration_worktree_name,
created_at, updated_at,
members[], tasks[], merged_task_ids[]
```

Member record 固定包含：

```text
member_id, name, role, backend(in_process),
state(idle|running|offline), current_task_id,
mailbox_cursor, created_at, updated_at
```

Task record 固定包含：

```text
task_id, title, instructions,
status(blocked|pending|running|completed|integrated|failed|cancelled),
assignee, dependencies[], created_at, updated_at,
started_at, ended_at, result, error_code,
workspace_path, workspace_preserved, workspace_reason,
branch, head, integrated_head
```

records 按 ID 排序；状态使用严格 JSON、重复 key 拒绝、同目录临时文件、flush、fsync 与 replace。lock 契约复用 Chapter 12 的 300 秒 + PID fail-closed 规则；manager 另有 asyncio lock。

状态损坏返回 `team_state_invalid`，不扫描 worktree、branch、mailbox 或 history 猜测修复。`active_team_id` 只能引用 `active` 或 `paused` team。

## 6. Mailbox 与成员历史

Mailbox 每行是精确 JSON：

```json
{
  "version": 1,
  "message_id": "<32hex>",
  "team_id": "<team_id>",
  "sender": "lead|<member name>|system",
  "recipient": "lead|<member name>",
  "kind": "message|assignment|result|system",
  "created_at": "<offset ISO8601>",
  "content": "<1..8192 code points>"
}
```

- 文件以 UTF-8 LF append、flush、fsync；单行最大 32 KiB；
- recipient 只能是 `lead` 或当前 team 的 member name；sender 同理加 `system`；
- 解析失败的最后一行视为崩溃残行并忽略；中间坏行返回 `team_mailbox_invalid`；
- mailbox 不自动截断、压缩、重写或删除；
- Member `mailbox_cursor` 是已确认处理的消息行数；dispatch 读取 cursor 之后的全部合法行，按文件顺序注入；
- 一个 episode 结束并持久化 task 终态/history 后才推进 cursor；因此崩溃可能重复，不会静默丢失；
- lead 通知使用独立 `recipient=lead` mailbox 与进程内 cursor；下一主 Agent request 消费后推进持久 cursor。

成员 history 每行只允许普通 `user` 或 `assistant` ChatMessage，不保存 tool call/result、thinking 或 controls。每个 task 终态追加：

1. `user`：精确 task title、instructions 与本次 mailbox；
2. `assistant`：成功 result，或仅含稳定 error code 的失败摘要。

下一 episode 只加载最后 `member_history_messages` 条，要求 user/assistant 成对；旧行保留不删除。

## 7. Team 创建与成员

`team_create` 只允许没有 active team 时调用：

1. 严格解析 team name 与非空 members；
2. 每个 member 精确包含 `name` 和 `role`；
3. role 必须在当前 WorkerCatalog 最终快照存在、`isolation: worktree`，且 role 的 Provider/tool 引用已通过 Chapter 11 启动校验；
4. 创建 `team_id` 和 member IDs；backend 固定 `in_process`；
5. 读取 main HEAD 为 `base_head`；
6. 用 Chapter 12 创建 manual integration worktree：`team/<team_id>/integration`；
7. 写 Team record、设 `active_team_id`，成员初始 idle；
8. 任一步失败不写半成品 Team；integration 创建后状态写失败时保留 worktree 并返回稳定错误，不 force 丢弃。

Team 创建后不 hot-add/remove member。本章通过关闭旧 team、创建新 team 改变 roster，避免运行中身份、DAG 和 mailbox 的歧义。

## 8. Task DAG

### 8.1 创建

`team_task action=create` 参数：

```text
title: 1..200，非空单行
instructions: 1..32768，保留原文
assignee: member name|null
depends_on: 唯一 task IDs 列表，最多 32
```

- dependencies 必须已存在于同一 active team；新 task 只指向旧 task，因此不会形成环；
- dependency 有 failed/cancelled 时新 task 仍为 blocked，不自动取消；Lead 可取消或创建替代 task；
- 全部依赖 completed/integrated 时是 pending，否则 blocked；
- 指定 assignee 只由该成员执行；null 由调度器选择按 member name 排序的首个 idle 成员；
- 达到 `max_tasks_per_team` 拒绝，不自动清理旧任务。

### 8.2 状态转换

```text
blocked -> pending
pending -> running | cancelled
running -> completed | failed | cancelled
completed -> integrated
```

- dependency 变为 completed/integrated 后重算 blocked；
- running cancel 通过 WorkerManager cancel，终态写 cancelled；
- terminal 不可重新运行；重试必须创建新 task 并显式依赖或引用旧 task；
- task `result` 最大 8000 code points，使用 Worker notification 相同头尾截断标记；
- `error_code` 只保存稳定码；
- workspace 字段来自 Chapter 12 Worker snapshot；没有 workspace 的团队 task 视为 backend contract failure。

## 9. Lead 调度与 in-process backend

TeamManager 启动命名 scheduler task，并提供 wake event。每次 tick 或状态变更：

1. 在 lock 内重载状态；paused/closed/merged 不调度；
2. 先把依赖满足的 blocked task 变 pending；
3. 按 `created_at, task_id` 排 ready tasks；
4. 按 member name 排 idle members；
5. assignee 精确匹配，否则取首个 idle；
6. 先原子写 task=running、member=running/current task，再启动 backend；
7. backend 启动失败回写 task=failed、member=idle；
8. 每个运行 episode 由 manager 持有 asyncio task，close 时先停止调度，再 cancel/gather；
9. 应用重启时，持久化 running task 没有可接管进程，固定转 failed=`team_member_interrupted`，member 转 idle，并向 lead mailbox 写 system 通知；不猜测旧进程仍存活。

in-process backend：

- 使用现有 WorkerCatalog、WorkerManager、WorkerExecutor 和 role definition；
- Worker task ID 直接使用 Team task ID；kind=`definition`，background=`true`，transition=`explicit`；
- parent_history 是该 member 最近的成对持久历史；
- task prompt 在原 instructions 前注入 team/member/task identity、dependency results 摘要和未确认 mailbox；
- visible tools 继续使用 Chapter 11 的 role/background 交集，不额外加入 `spawn_worker` 或 Team 管理工具；
- 等待 Worker terminal，不把它转前台；
- 完成后读取 workspace snapshot 和实时 worktree status，保存 result/head/branch；
- backend protocol 只暴露 `start`, `cancel`, `close` 与 terminal result，不暴露 Worker 内部对象。

## 10. Provider 工具

固定工具名加入系统保留集合：

### `team_create`

```json
{
  "name": "release-team",
  "members": [
    {"name": "implementer", "role": "general"},
    {"name": "reviewer", "role": "verify"}
  ]
}
```

创建 active team。members 为 `1..max_members_per_team`，对象只接受 `name/role`。

### `team_task`

```text
action=create: title, instructions, assignee?, depends_on?
action=list:  无其他字段
action=get:   task_id
action=cancel: task_id
```

所有 action 与字段精确匹配；list/get 返回脱敏持久状态，不返回完整 member history/mailbox。

### `team_message`

```json
{"recipient":"reviewer","content":"优先检查 cache invalidation。"}
```

只从 Lead 发送到成员 mailbox。不能伪造 sender、message ID 或时间。

### `team_status`

无参数，返回 active team、member occupancy、task counts 和 integration 状态摘要。

### `team_integrate`

```json
{"task_id":"<32hex>"}
```

只把一个 completed task branch 合并进 integration；不合并 main、不自动删除 task worktree。

Team 工具只注册在主 Agent ToolRegistry。Worker 可见集合无论 role 声明都移除全部 Team 管理工具；加载/扫描时引用这些保留工具 fail-fast。

## 11. 通知

Task terminal 与 integrate terminal 各 append 一条 lead mailbox result/system 消息。主 Agent 下一 request 前，TeamManager 返回尚未确认的 lead 消息并形成 request controls：

```json
{
  "type": "team_notification",
  "team_id": "...",
  "message_id": "...",
  "sender": "member|system",
  "kind": "result|system",
  "content": "..."
}
```

- 单条 content 最大 8000，总批次最大 32 条；
- request control 构造成功后持久推进 lead cursor；
- 不写 ConversationHistory、session JSONL、摘要或 notes；
- session 切换不丢通知，因为 Team 属于项目而非 session；
- 同一消息最多由正常消费路径返回一次；崩溃窗口允许下一进程重复。

## 12. Integration 与 main

### 12.1 Task 集成

`team_integrate(task_id)`：

1. active/paused team 均可，task 必须 completed；
2. task workspace record 必须存在且 name=`worker/<task_id>`；
3. 纯文件恢复验证 branch，实时 status 必须 exists、clean、无 status error；`has_unpushed=true` 是预期，因为 commit 尚未 push；
4. HEAD 必须不同于 task `base_head`，且等于持久 task `head`；
5. integration worktree 实时 status 必须 clean；
6. 执行 `git -C <integration> merge --no-ff --no-edit <task branch>`；
7. 非零时执行 `git merge --abort`；无论 abort 是否成功都返回 `team_merge_conflict`，不声称恢复；
8. 成功后保存 integration HEAD、task=integrated、merged_task_ids；
9. 不删除 member worktree/branch，不 push。

重复 integrate 同一 task 是 idempotent，只返回保存的 integrated state，不再次运行 Git。

### 12.2 合并 main

只能由 `/team merge --into-main` 调用：

1. TeamManager 生成 preview：team/name、main/integration path、main/integration HEAD、task counts、dirty booleans；任一 status error 不显示确认并拒绝；
2. 要求没有 running/pending/blocked/failed task；completed 必须先 integrated；cancelled 可忽略；
3. UI 显示“会更新主工作树且可能触发 Git hooks”，要求 destructive confirmation；
4. 确认后重新检查 main 与 integration clean；
5. 在 integration 执行 `git merge --no-edit <current main HEAD>`，冲突只在 integration，失败尝试 abort；
6. 重新读取 main HEAD，必须仍等于步骤 4；
7. 在 main 执行 `git merge --ff-only <integration branch>`；
8. 成功写 team=merged、active_team_id=null；
9. 不自动删除任何 worktree/branch、不 push。

main ff-only 失败返回 `team_main_changed` 或 `team_merge_failed`，integration 保留；不 reset main。

## 13. 命令

| 命令 | 行为 |
| --- | --- |
| `/teams` | 列出持久 team 摘要 |
| `/team show [team_id]` | 显示 active 或指定 team/member/task count |
| `/team pause` | active→paused，运行中 task 继续到终态，不启动新 task |
| `/team resume` | paused→active 并唤醒调度 |
| `/team close` | 确认后停止新调度、cancel/gather running task、写 closed 与 active=null；不删除文件/worktree |
| `/team merge --into-main` | preview、破坏性确认、integration 吸收 main 后 ff-only 更新 main |

命令本地执行，不进入普通历史。命令名大小写不敏感；子命令、team ID 与 `--into-main` 精确匹配。

## 14. 恢复与关闭

启动：

1. 非 Git 或 Team state invalid 时 Team 子系统 unavailable，普通 Agent 仍可运行，Team 工具/命令返回稳定码；非法用户配置仍 fail-fast；
2. active/paused team 的 role 必须仍存在且保持 `isolation: worktree`，否则 team 标记 paused 并返回 `team_role_unavailable`，不替换角色；
3. running task 转 failed=`team_member_interrupted`，member idle，写 lead system message；
4. active team 启动 scheduler，paused 不启动任务但可 list/show/message/integrate；
5. integration worktree 使用 Chapter 12 fast recover 验证；失败 Team unavailable，不自动修复。

关闭顺序：

1. TeamManager 停止接收新操作与 scheduler；
2. backend cancel 所有运行 member episode；
3. gather 并持久化 cancelled=`team_shutdown`；
4. 关闭 backend；
5. 再关闭 WorkerManager、WorktreeManager、Notes、Hook、Session、MCP 与 artifact；
6. close 幂等返回 active/cancelled/persisted 统计。

应用正常退出不会把 active team 改 closed；下次启动可继续 pending/blocked DAG。

## 15. 错误码

```text
team_config_invalid
team_state_invalid
team_state_locked
team_repository_unavailable
team_not_found
team_active_exists
team_name_invalid
team_member_invalid
team_member_not_found
team_role_unavailable
team_capacity_reached
team_task_invalid
team_task_not_found
team_task_terminal
team_dependency_invalid
team_mailbox_invalid
team_backend_failed
team_member_interrupted
team_paused
team_closed
team_integration_unsafe
team_merge_conflict
team_merge_failed
team_main_changed
team_shutdown
```

错误消息不包含 instructions、mailbox content、result、history、工具数据、Git stderr、Provider 数据、secret、exception repr 或 traceback。

## 16. 安全与权限

- Team 管理工具是主 Agent 系统工具；成员工具集合强制移除 `team_*` 和 `spawn_worker`；
- task instructions、mailbox 和历史都是不可信 Prompt 数据，不能授予权限、改变 role 白名单或越过 Hook/PathSandbox；
- in-process member 沿用 Chapter 11 无 UI 审批，`ask` 固定 reject；
- task worktree 沿用 Chapter 12 ContextVar 根隔离；这不是网络、进程、home directory 或 OS sandbox；
- integration/main merge 是 Git side effect，可能执行仓库 hooks；task integrate 只影响 isolated integration，main merge 必须用户确认；
- 所有 merge 参数来自持久化且重新验证的内部 branch/path，不接受用户任意 ref/path；
- mailbox/history JSON content 永不作为命令、路径、field name 或 identifier 解析；
- 不自动 push、PR、commit、reset、clean、force update main 或删除用户数据。

## 17. 测试矩阵

### 配置、模型与存储

- config 默认、重复/未知/缺失字段、bool/range/even；
- name/ID/reserved、team/member/task 状态组合、唯一性与排序；
- strict JSON、atomic replace、live/stale/unknown PID lock；
- mailbox append、LF、size、final partial、middle corrupt、sender/recipient；
- member history 成对、tail limit、不自动删除。

### DAG 与调度

- create active conflict、role missing/isolation none/member duplicate/capacity；
- dependency missing、blocked→pending、failed dependency保持 blocked、deterministic ready/member order；
- explicit assignee、single member ownership、single task execution、backend start failure；
- success/failure/cancel/timeout、result truncation、workspace required；
- pause stops new starts but running finishes；resume wakes；close cancel/gather/idempotent；
- restart converts running to interrupted and preserves pending DAG。

### Mailbox、历史与通知

- lead→member append、next episode injection、cursor after terminal、crash redelivery；
- task/result history append、下一 episode只携带 tail paired history；
- terminal/integrate lead message、batch 32、persistent cursor、no ConversationHistory write；
- member cannot see Team tools or forge sender/ID/time。

### Worktree 与 merge

- team create integration worktree、state failure preserve；
- completed task requires workspace/branch/head/clean；dirty/status error/detached/missing record refuse；
- task merge success/idempotent/conflict abort/abort failure；
- main preview task completeness和 clean；confirmation cancel no Git；
- integration absorbs current main、main race、ff-only success/failure；
- no task/integration worktree auto delete、no push/remote/PR。

### 工具、命令、CLI 与回归

- 五个固定 tools schema/action strict/error mapping；
- `/teams`、show/pause/resume/close/merge 参数与确认；
- CLI startup/recovery/notification/close order；
- Chapter 01–12 全量测试继续通过；
- wheel 包含 teams 模块；
- 默认测试只使用临时 Git 仓库、本地 fake backend/Provider，不访问公网。

## 18. 验收标准

1. Team/Member/Task/Message 身份与状态可跨应用重启精确恢复，不靠目录扫描或文本猜测。
2. DAG 只调度 dependencies 完成的 task；同一 member/task 没有重复并发 owner。
3. mailbox append、cursor 和 history 提供崩溃后至少一次投递，不静默丢消息且不自动删除。
4. in-process backend 真实执行现有定义式 Worker，并保持 role、权限、工具与 worktree 隔离。
5. terminal 结果、workspace 与通知持久化；主 Agent 下一 request 可见且不污染普通历史。
6. dirty/status error/branch/head 不一致时 task integration fail-closed；conflict 不触碰 main。
7. main 只有在用户确认、任务完整、双工作树 clean 且最终 ff-only 时更新。
8. pause/resume/close/restart 的状态转换确定且 close 能 cancel/gather，无孤儿 asyncio task。
9. closed team、mailbox、history 和 worktree 不自动清理；不自动 push/PR/commit/reset/clean。
10. pane backend 未实现的事实在 API、README 和 evaluation 中明确，不返回伪成功。
