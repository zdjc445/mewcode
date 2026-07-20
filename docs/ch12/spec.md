# Chapter 12 Specification：Git Worktree 隔离、切换与恢复

## 1. 目标与边界

本章在 Chapter 11 Worker 之下增加 Git 原生 linked worktree 隔离，并提供可恢复的手动管理入口。目标是让独立任务拥有自己的分支和工作目录，同时保持删除 fail-closed、路径不可逃逸、运行时缓存不串目录。

本章完成：

1. 严格名称、用户配置、Git 命令执行、状态持久化和崩溃恢复；
2. create、fast recover、enter、exit、status、delete 生命周期；
3. 本地配置复制、主仓库 hooksPath 继承、依赖目录链接和 ignored 文件复制；
4. 脏修改与未推送提交保护；
5. `isolation: worktree` Worker 自动创建、绑定、提示和安全收尾；
6. `/worktrees` 与 `/worktree` 命令及 `--resume`；
7. 周期清理仅处理本模块登记且安全检查通过的过期临时 worktree。

本章不完成：

- 远端自动 push、PR 或分支合并；
- 容器、虚拟机或操作系统级隔离；
- 跨主机共享同一状态文件；
- Chapter 13 Team 的多成员合并编排；
- 自动丢弃脏修改或未推送提交。

## 2. 核心不变量

1. 所有管理目录都位于启动 Git 主 worktree 的 `<main_root>/.mewcode/.worktrees/` 内。
2. 所有规范名称都先严格解析，再参与任何路径或分支计算；不 lower、不修复、不猜测。
3. Git 子进程固定使用 argv、`shell=False`、绝对 `-C` 路径和 timeout；stderr 不进入模型或状态文件。
4. 删除前必须重新检查 dirty 与 unpushed；任一 Git 检查失败都拒绝删除。
5. 周期清理不会使用目录扫描结果作为删除授权，只处理状态文件中的 managed 记录。
6. Worker 与手动进入都通过显式工作目录绑定；共享工具不能沿用主目录 PathSandbox。
7. 手动 enter/exit 通过关闭并重建应用运行时完成，不在活动 Agent request 中原地替换依赖。
8. 自动 Worker 收尾只删除完全干净且没有未推送提交的 worktree；否则保留。
9. 本章不自动 push、commit、merge、reset、clean 或丢弃用户修改。
10. 状态、配置、复制诊断和 Git 错误都不保存 API key、文件正文、命令 stderr 或 Hook 数据。

## 3. 模块结构

```text
src/mewcode_agent/worktrees/
├── __init__.py
├── models.py       # 名称、配置、记录、状态、status 与 close 统计
├── loader.py       # 严格 workers.yaml 风格 YAML 配置
├── git.py          # 无 shell Git runner、porcelain 解析与纯文件 HEAD
├── storage.py      # JSON 状态、lock file 与原子 replace
├── initializer.py # hooks、local config、ignored copy、dependency links
├── manager.py      # 生命周期、保护、恢复与周期清理
├── runtime.py      # PathSandbox/Hook/Prompt 的 worktree binding
└── commands.py     # `/worktrees` 与 `/worktree`
```

现有模块改动：

| 模块 | 改动 |
| --- | --- |
| `security.path_sandbox` | 默认根目录 + ContextVar 临时根目录 |
| `hooks.actions` | shell cwd 使用 ContextVar 工作目录 |
| `hooks.engine` | `project.root` 使用相同工作目录绑定 |
| `prompting.runtime` | fork 时可替换 session environment 与 request collector |
| `workers.executor` | worktree provision、绑定、路径翻译 Prompt 与收尾 |
| `workers.tools` | Chapter 12 起接受 `isolation: worktree` |
| `app` | CommandUI 提供受控 workspace restart 请求 |
| `cli` | `--resume`、运行时重建、manager/commands 装配与关闭 |

## 4. Git 仓库身份与目录

### 4.1 主仓库

启动时执行：

```text
git -C <startup_directory> rev-parse --show-toplevel
git -C <startup_directory> rev-parse --git-common-dir
```

- 两条命令都必须成功并返回有效 UTF-8；
- main root 取第一条规范化绝对目录；
- common git dir 按第二条相对 main root 解析并要求为现有目录；
- 若当前启动目录本身是本模块 managed worktree，则通过持久化记录恢复其 `main_root`，不把 linked root 当成新的管理根；
- 非 Git 仓库启动时 Worktree 子系统标记 unavailable，普通 Agent 仍可启动，worktree 命令返回 `worktree_repository_unavailable`，`isolation: worktree` Worker 返回同码。

### 4.2 管理路径

```text
managed root: <main_root>/.mewcode/.worktrees/
state:        <common_git_dir>/mewcode-agent/worktrees.json
lock:         <common_git_dir>/mewcode-agent/worktrees.lock
```

Manager 初始化时把精确行 `/.mewcode/.worktrees/` 以 UTF-8 LF 幂等加入 `<common_git_dir>/info/exclude`。不修改项目 tracked `.gitignore`。无法读取或写入 exclude 时拒绝创建，不影响只读 list/status。

## 5. 名称、路径与分支

规范名称最多 `4` 段，以 `/` 分隔；总长度 `1..96` Unicode code point。每段必须完整匹配：

```regex
[a-z][a-z0-9_-]{0,31}
```

额外规则：

- 不接受空段、`.`、`..`、反斜杠、盘符、绝对路径、前后 `/` 或重复 `/`；
- 不进行 Unicode normalization、URL decode、大小写转换或分隔符替换；
- Windows 保留设备名按不区分大小写精确拒绝：`CON`、`PRN`、`AUX`、`NUL`、`COM1..9`、`LPT1..9`；
- 最终目录为 `managed_root / segment1 / ...`，resolve 后必须仍在 managed root 内；
- 一个名称唯一映射一个目录。

分支名不直接使用可能嵌套的名称。固定为：

```text
mewcode-wt-<segments joined by ->-<sha256(name) first 12 hex>
```

分支总长超过 `120` 时先把 joined 部分截到能容纳固定前后缀，再附 hash。hash 消除 `a/b` 与 `a-b` 等平铺碰撞。不接受用户提供任意 branch 名。

Worker 自动名称固定为 `worker/<task_id>`；`task_id` 已是 32 位小写十六进制。

## 6. 用户配置

只读取：

```text
~/.mewcode-agent/worktrees.yaml
```

项目不能改变复制、链接或清理策略。文件不存在使用内存默认值且不创建。存在时必须是精确结构：

```yaml
version: 1
stale_after_hours: 72
cleanup_interval_seconds: 1800
local_config_files:
  - settings.local.json
dependency_links: []
copy_ignored: []
```

约束：

- `version` 是整数 `1`；
- `stale_after_hours` 是 `1..8760` 整数；
- `cleanup_interval_seconds` 是 `60..86400` 整数；
- 三个路径列表保持顺序、非重复，每项是 UTF-8 POSIX 相对路径；
- 路径不为空，不以 `/` 结尾，不含 `.`、`..`、空段、反斜杠或 NUL；
- 路径不得位于 `.git` 或 `.mewcode/.worktrees` 下；
- `local_config_files` 与 `copy_ignored` 可指文件或目录；`dependency_links` 只指目录；
- 默认只尝试复制根目录 `settings.local.json`，依赖与 ignored 列表为空。

配置变更需重启应用。初始化动作都是 best-effort，但配置结构和路径本身不是 best-effort，非法即启动 fail-fast。

## 7. 状态文件与锁

状态 JSON schema version 固定为 `1`：

```json
{
  "version": 1,
  "main_root": "<absolute>",
  "active_name": null,
  "records": [
    {
      "name": "worker/abc...",
      "path": "<absolute>",
      "branch": "mewcode-wt-worker-abc-<hash>",
      "base_head": "<40-or-64-lower-hex>",
      "kind": "manual|worker",
      "owner_id": null,
      "created_at": "<timezone ISO 8601>",
      "last_used_at": "<timezone ISO 8601>",
      "expires_at": "<timezone ISO 8601>",
      "initialization_diagnostics": []
    }
  ]
}
```

- records 按 `name` 排序；name、path 和 branch 分别唯一；
- worker record 的 `owner_id` 必须是同一 task ID；manual 必须为 null；
- path 必须等于名称的规范映射，不能由状态文件任意指定；
- 时间必须带 offset；`created_at <= last_used_at <= expires_at`；
- 状态写入使用同目录临时文件、flush、`os.fsync` 和 `os.replace`；
- 不完整 JSON、未知字段、路径不一致或重复记录返回 `worktree_state_invalid`，不扫描目录猜测修复；
- 写事务先用 `O_CREAT|O_EXCL` 创建 lock；lock 内容只含 PID 与创建时间；
- lock 未过期时返回 `worktree_state_locked`；超过 `300` 秒且 PID 不存在时才允许回收；PID 存活无法确认时 fail-closed；
- 进程内另有 `asyncio.Lock` 串行化 manager 操作。

`active_name` 表示上次受控 enter 的目标。exit 写 null。`--resume` 只接受状态中存在且目录验证成功的 active record。

## 8. Git 执行与错误

Git runner 固定：

- 用 `shutil.which("git")` 解析一次绝对 executable；
- `asyncio.create_subprocess_exec(git, "-C", absolute_cwd, ...)`；
- stdout/stderr 上限各 `1 MiB`，解码 UTF-8 strict；
- 默认 timeout `30` 秒，worktree add/remove `120` 秒；
- timeout/cancel 时 terminate，等待 `2` 秒，再 kill 并 reap；
- 返回码非零只映射阶段化错误码，不保存 stderr；
- 不使用 shell、用户 alias 文本、环境拼接或命令字符串。

稳定阶段码包括：

```text
worktree_git_unavailable
worktree_repository_unavailable
worktree_git_timeout
worktree_create_failed
worktree_status_failed
worktree_remove_failed
worktree_hooks_failed
```

## 9. 创建与快速恢复

### 9.1 新建

在 manager lock 内：

1. 解析名称并计算 path/branch；
2. 确认状态无同名、目标路径不存在；
3. 读取 main root 当前 `HEAD` 为 `base_head`；
4. 确认分支不存在；
5. 确保 info/exclude；
6. 执行 `git -C <main_root> worktree add -b <branch> <path> <base_head>`；
7. 验证目标 `.git` 是 linked-worktree 文件、HEAD 等于 base_head；
8. 运行环境初始化；
9. 写入 record；
10. 若 8 或 9 失败，尝试 `git worktree remove --force <path>` 与删除新分支；清理失败记录诊断并返回创建失败，不声称回滚成功。

不允许从 dirty index、未提交文件或任意 ref 创建；新 worktree 始终从已提交 HEAD。

### 9.2 快速恢复

当状态已有 record 且目录存在时，`create(name)` 不启动任何 Git 子进程。它只：

1. 验证规范 path 与 `.git` 文件；
2. 解析 `.git` 文件的精确 `gitdir: <absolute-or-relative>`；
3. 纯文件系统读取 `<gitdir>/HEAD`；
4. 若 HEAD 是 `ref: refs/heads/...`，从 common loose ref 或 `packed-refs` 精确解析；
5. 验证 ref 为 record branch 且 object ID 为 40 或 64 位小写 hex；
6. 更新 `last_used_at` 与 `expires_at` 并返回 `recovered=true`。

任何不一致返回 `worktree_recovery_failed`；不会调用 Git 猜测或自动修复。

状态无记录但目录已存在时固定返回 `worktree_path_conflict`。

## 10. 环境初始化

初始化按顺序执行，各步骤产生只含 path 与稳定 code 的诊断；单步失败不回滚已成功 Git worktree：

1. `local_config_files`：源存在时用 `shutil.copy2` 或 `copytree(symlinks=False)` 复制；目标已存在跳过；
2. hooks：读取 main root 的 `core.hooksPath`；缺失则跳过；存在时解析为绝对路径，执行 `git config extensions.worktreeConfig true`，再在目标执行 `git config --worktree core.hooksPath <absolute>`；
3. `dependency_links`：源必须是 main root 内真实目录；目标不存在时创建目录 symlink；Windows 不具备权限时记录 `worktree_dependency_link_failed`，不回退为全量复制；
4. `copy_ignored`：先执行 `git -C <main_root> check-ignore -q -- <exact-relative>`；只有返回 0 才复制，返回 1 记录 not-ignored 跳过，其他返回码记录检查失败；
5. 所有父目录按需创建，但 resolve 后必须留在目标 root；复制不跟随源符号链接越过 main root。

诊断保存于 record；不保存文件正文、symlink target 之外的环境信息或 stderr。

## 11. Status 与删除保护

### 11.1 Status

每次 status 都实时执行：

```text
git status --porcelain=v1 -z --untracked-files=all
git rev-parse HEAD
git rev-parse --abbrev-ref --symbolic-full-name @{upstream}
git rev-list --count @{upstream}..HEAD
```

输出模型：

```text
exists
head
dirty
dirty_entry_count
upstream (string|null)
unpushed_commit_count (int|null)
has_unpushed
deletion_safe
reason_code (string|null)
```

规则：

- porcelain 非空即 dirty，不解析或展示文件名；
- 有 upstream 时按 rev-list count 判断 unpushed；
- 无 upstream 且 `HEAD != base_head` 时 `has_unpushed=true`、count=null；
- 无 upstream 且 `HEAD == base_head` 时无未推送提交；
- 任一命令、UTF-8 或数字解析失败返回 status error，`deletion_safe=false`；
- `deletion_safe = exists and not dirty and not has_unpushed and no error`。

### 11.2 删除

普通 delete：

- active record、当前 Worker owner 正在使用或 status 不安全时拒绝；
- 安全时执行 `git worktree remove <path>`，再 `git branch -d <branch>`；
- 任一步失败保留 record 并返回错误；下次可重试或 fast recover。

force delete 只由确认后的 `/worktree delete <name> --discard` 调用：

- 再次读取 status 并把 dirty/unpushed 摘要展示在 ConfirmationRequest；
- 确认后执行 `git worktree remove --force <path>` 与 `git branch -D <branch>`；
- force 仍不绕过规范路径、active/owner 检查或 Git status 无法确认；
- 删除成功才移除 record；
- 明确告知该操作不可恢复。

## 12. 手动 enter、exit 与 runtime rebuild

命令只允许在没有活动 Agent request 时执行。流程：

### enter

1. 验证 record 与 fast-recovery HEAD；
2. 状态写 `active_name=name` 并更新 last-used/expiry；
3. CommandUI 记录绝对 restart target；
4. TUI 正常退出；
5. CLI 固定先关闭 WorkerManager、Notes、Hook、Session、MCP 与 artifact；
6. 在同一 Python 进程中用 target 重新执行完整 application bootstrap。

### exit

1. 读取 state.main_root；
2. 写 `active_name=null`；
3. 请求 runtime restart 到 main root；
4. 使用相同关闭与重建流程。

重建会自然清除并重载：

- FileStateCache；
- PromptRuntime 与项目/用户指令；
- 项目/用户 notes；
- Skill/Worker catalog 与 active Skill；
- Hook configuration、once 状态与 shell cwd；
- PathSandbox、security project rules、MCP cwd、Git environment collector；
- session manager 的 project root。

这比逐对象 mutation 更易验证，也保证旧对象不能处理新目录请求。restart 不是新 OS 进程，不丢失 API 环境变量。

### `--resume`

CLI 只识别唯一可选参数 `--resume`。存在时：

- 从启动目录解析 main root/common dir/state；
- active_name 为 null 时从 main root 启动；
- active record 通过纯文件恢复校验后从 record path 启动；
- 无效状态返回启动错误，不回退或猜测其他目录。

## 13. Worker worktree 隔离

角色 `isolation: worktree` 的定义式 Worker 启动流程：

1. name 固定 `worker/<task_id>`、kind=worker、owner_id=task_id；
2. WorkerExecutor 在首轮前 `create`；
3. 使用 ContextVar 把共享 PathSandbox、Hook project root/shell cwd 和 request Git collector 绑定到 worktree path；
4. PromptRuntime session environment 的 `working_directory` 替换为 worktree path；
5. FileStateCache 仍使用 Chapter 11 每 task 独立 mapping；
6. 首条 user task 前加路径翻译说明：主目录与 worktree 的绝对路径、所有相对路径基于 worktree、不得修改 main root；
7. 可见工具、权限、Hook、Token 与后台规则保持 Chapter 11 契约；
8. terminal 后读取实时 status；安全则自动 delete；dirty、unpushed 或 status error 则保留并在 Worker record/notification 中写 `workspace_preserved=true` 与稳定 reason；
9. cancel 同样执行保护式收尾，不 force delete；
10. Fork 不自动启用 worktree，因为 Fork 没有角色；Hook none/summary 使用 general 的 isolation，recent Fork 不隔离。

`SpawnWorkerTool` 不再返回 `worker_isolation_unavailable`。create 失败使前台返回 `worker_failed`，后台记录保存精确 worktree error code。

## 14. ContextVar 目录绑定

### PathSandbox

- 实例保留默认 main working directory；
- `bind_working_directory(path)` 验证 path 是绝对现有目录并创建 ContextVar token；
- `working_directory` 和所有 resolve 每次读取当前 binding；
- 子 asyncio task 继承 binding；退出后 reset；
- 主 Agent 不绑定时继续使用默认目录。

### Hook

`HookActionRunner.bind_project_root(path)` 与 `HookEngine.bind_project_root(path)` 使用同样模式。Worker task 内创建的 async Hook task 继承绑定。shell cwd 与 `project.root` 字段来自当前 binding。退出后不能落到 worktree 之外继续执行同步动作。

### Prompt environment

Worker 使用指向 worktree 的新 `SessionEnvironment` 与 `GitRequestEnvironmentCollector`，不修改主 runtime。手动 enter/exit 通过 bootstrap 重建。

## 15. 周期清理

Manager 启动一个命名 background task，每 `cleanup_interval_seconds` 执行一次；close 时 cancel 并 gather。每个候选必须依次通过：

1. record.kind 是 `worker`；manual 永不自动清理；
2. 名称重新解析且 path 精确匹配；
3. 非 active、无当前 owner、`now >= expires_at`；
4. 目录存在且 linked `.git` 文件通过纯文件验证；
5. 实时 status 成功且 deletion_safe；
6. 普通非 force delete。

任一步失败只更新脱敏 cleanup diagnostic/last-attempt，不删除。目录扫描发现但 state 未登记的路径永不自动处理。用户修改不会因过期被删除。

## 16. 命令

公开命令：

| 命令 | 行为 |
| --- | --- |
| `/worktrees` | 列出 managed records 与实时 status 摘要 |
| `/worktree create <name>` | 创建或快速恢复 manual worktree |
| `/worktree enter <name>` | 持久化 active 并请求 runtime rebuild |
| `/worktree exit` | 返回 main root 并请求 runtime rebuild |
| `/worktree status [name]` | 显示当前或指定 record 的完整脱敏状态 |
| `/worktree delete <name>` | 只删除安全 worktree |
| `/worktree delete <name> --discard` | 展示 dirty/unpushed 摘要，确认后 force 删除 |

命令名大小写不敏感；子命令、name 和 `--discard` 精确匹配。所有命令本地执行，不写普通历史。创建不自动 enter。enter/exit 若已在目标位置返回 no-op 状态而不重建。

## 17. 错误码

```text
worktree_name_invalid
worktree_config_invalid
worktree_state_invalid
worktree_state_locked
worktree_repository_unavailable
worktree_git_unavailable
worktree_git_timeout
worktree_not_found
worktree_already_exists
worktree_path_conflict
worktree_branch_conflict
worktree_create_failed
worktree_recovery_failed
worktree_initialization_failed
worktree_status_failed
worktree_delete_unsafe
worktree_in_use
worktree_remove_failed
worktree_restart_failed
```

错误消息不包含 Git stderr、文件正文、复制内容、Hook 数据、环境变量或 exception repr。命令可显示规范 name/path/branch、dirty boolean/count、unpushed count 与稳定 reason。

## 18. 安全与并发

- 所有 filesystem delete 目标先由 record name 重新计算并 resolve 验证；不接受状态文件中的任意 path；
- 删除只委托 `git worktree remove`；初始化失败清理也不递归删除未知目录；
- 复制与链接拒绝源/目标 symlink 越界；
- Manager 的 asyncio lock 防同进程并发，lock file 防两个 CLI 同时写状态；
- read-only list/status 在 state lock 外读取快照，但输出前验证 schema；
- Git hooks 仍是仓库作者代码，创建/提交等 Git 行为可能执行 hooks；本章不把 hooks 视为受安全策略审批的 Tool；
- worktree 隔离只隔离 Git working tree/index/branch，不隔离网络、进程、用户主目录或仓库外路径；PathSandbox 继续限制内置工具在当前 worktree 内。

## 19. 测试矩阵

### 名称与配置

- 有效嵌套名称、总长/段长边界、空段、`.`/`..`、反斜杠、绝对路径、设备名；
- branch flatten hash 无碰撞；
- 配置默认不写盘、重复键、未知/缺失字段、整数/bool、路径越界与重复。

### 状态与纯文件恢复

- 严格 JSON、重复 record/path/branch、path 重算、时间、owner；
- atomic replace、live/stale lock、PID 无法确认 fail-closed；
- `.git` 文件、loose ref、packed-refs、detached HEAD、错误 ref 与无 Git 子进程 fast recover。

### Git 生命周期

- argv/cwd/timeout/cancel/reap/stderr 脱敏；
- create 成功、branch/path conflict、初始化诊断、状态写失败回滚；
- dirty、upstream ahead、无 upstream + new commit、无 upstream + base HEAD；
- safe delete、unsafe refuse、确认 force、active/owner refuse、remove failure 保留 record。

### 初始化

- settings copy、目录 copy、existing destination skip；
- hooksPath absent/relative/absolute 与 worktree config；
- dependency symlink success/Windows 权限失败不复制；
- ignored=0、not ignored=1、检查错误与 symlink escape。

### runtime 与 Worker

- PathSandbox/Hook/Prompt binding 在并发 Worker 间隔离并在退出 reset；
- worktree Worker 首请求路径翻译、session/request cwd、工具真实落在隔离目录；
- clean/no-commit 自动删除；dirty、unpushed、status error 保留；
- cancel/timeout/background notification 包含保留状态；Fork 不自动隔离；
- Worker 内 async Hook 继承 worktree cwd。

### 命令、恢复与清理

- create/list/status/enter/exit/delete 参数严格且不进历史；
- restart 先完整关闭旧 runtime，再构造新 root；
- `--resume` active/null/invalid；
- manual 永不周期清理；worker 未过期、active、owner、dirty、unpushed、status failure 不清理；只有登记、过期、安全候选删除；
- close cancel/gather cleanup task 且幂等。

### 回归

- Chapter 01–11 全量测试继续通过；
- wheel 包含 Worktree 模块；
- 不连接远端、不 push、不 merge、不创建 PR；
- 默认测试只在 pytest 临时 Git 仓库内创建/删除 worktree。

## 20. 验收标准

1. 名称无法越过 managed root，branch 映射稳定无歧义。
2. create 与 fast recover 可区分，fast recover 不启动 Git。
3. dirty、unpushed 或检查失败时普通与自动删除都 fail-closed。
4. 初始化错误可诊断但不伪造成功，依赖链接失败不偷偷复制大目录。
5. 手动 enter/exit 通过 runtime rebuild 清除全部目录相关缓存，`--resume` 可恢复。
6. `isolation: worktree` Worker 的内置文件和命令工具真实绑定隔离根目录。
7. clean Worker 自动收尾，任何真实变更或检查不确定时保留。
8. 周期清理只处理登记、过期、未使用且安全的 worker records，永不处理 manual 或未知目录。
9. 命令、状态、错误与日志不泄露正文、stderr、secret 或 traceback。
10. 不自动 push、commit、merge、reset、clean 或丢弃用户修改。
