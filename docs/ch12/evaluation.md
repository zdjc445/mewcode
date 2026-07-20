# Chapter 12 Evaluation：Git Worktree 隔离、切换与恢复

## 验收环境

| 字段 | 实际值 |
| --- | --- |
| 时间 | `2026-07-21T03:03:12.8964001+08:00` |
| 分支 | `master` |
| 验收代码基线 Commit | `b517f14` |
| 真实 API 条件 | `DEEPSEEK_API_KEY` 未设置 |
| 代码状态 | 本验收记录尚未提交，其余 Chapter 12 代码、测试、README 与入口配置已提交并推送 |

## 分批提交记录

| Commit | 内容 |
| --- | --- |
| `822116e` | Chapter 12 Git Worktree 隔离、切换与恢复规格 |
| `8e9acc2` | 严格名称/配置/状态、跨进程锁、无 shell Git runner 与纯文件 HEAD 恢复基础层 |
| `7657c7e` | 环境初始化、create/fast recover、status、删除保护、active/owner 防护与周期安全清理 |
| `a56c876` | PathSandbox/Hook/Prompt ContextVar 绑定、worktree Worker、保留状态与 CLI 装配 |
| `82540f1` | 本地命令、discard 确认、受控 TUI restart、同进程完整 bootstrap 与 `--resume` |
| `b517f14` | Git stdout/stderr 分块硬上限与启动环境错误脱敏 |

## 自动化结果

| 检查 | 实际结果 |
| --- | --- |
| `.venv\\Scripts\\python.exe -m pytest -q` | `964 passed, 5 skipped in 40.64s`，exit code `0` |
| Git runner/Manager/Worker/CLI 聚焦回归 | `63 passed in 22.98s`，exit code `0` |
| Chapter 12 + Worker/Hook/App/CLI 聚焦回归（硬上限修正前） | `271 passed, 2 skipped in 36.55s`，exit code `0`；随后新增硬上限测试并由全量回归覆盖 |
| `python -m compileall -q src tests integration_tests` | exit code `0` |
| `git diff --check` | exit code `0`；只有 Git 的 LF→CRLF 工作区提示 |
| `uv build --wheel` | 成功生成 `dist/mewcode_agent-0.1.0-py3-none-any.whl` |
| wheel 内容检查 | `mewcode_agent/worktrees/` 的 9 个模块全部存在；console entry point 为 `mewcode_agent.cli:console_main` |

五个跳过项为：

- `tests/test_instruction_loader.py::test_symlink_outside_root_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_notes_storage.py::test_project_notes_symlink_escape_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_security_boundary.py::test_path_sandbox_rejects_symlink_escape`：当前 Windows 环境不允许测试创建目录符号链接；
- `tests/test_session_storage.py::test_session_permissions_are_private_on_posix`：只适用于 POSIX 权限契约；
- `tests/test_worktree_initializer.py::test_source_symlink_escape_is_refused`：当前 Windows 环境不允许测试创建文件符号链接。

## 已验证场景

### 名称、配置、状态与 Git 边界

- worktree 名称的段数、段长、总长、空段、`.`、`..`、反斜杠、绝对路径、盘符与 Windows 设备名均按规格严格处理；不 lower、不修复；
- `a/b` 与 `a-b` 映射到不同 hash 分支；分支名称稳定且不超过 120 code points；目录只由受管 root 与规范 name 计算；
- 用户 `worktrees.yaml` 不存在时只使用内存默认值；存在时严格拒绝重复键、未知/缺失字段、bool 伪整数、越界/重复路径与受保护目录；
- 状态 JSON 拒绝重复键、未知字段、非规范 main/path、重复 name/path/branch、无 offset 时间与 owner/name 不一致；写入使用同目录临时文件、flush、fsync 和 replace；
- lock 使用 `O_CREAT|O_EXCL`；live PID、未过期、PID 无法确认和损坏锁均 fail-closed；只有超过 300 秒且确认 PID 不存在时回收；
- Git 固定使用解析后的绝对 executable、argv 与绝对 `-C`，不使用 shell；stdout/stderr 并发分块读取并在各自超过 1 MiB 时立即终止、等待并回收子进程；timeout/cancel 同样 terminate、等待、必要时 kill；错误不包含 stderr；
- linked `.git`、HEAD、loose ref 与 `packed-refs` 均由纯文件系统严格解析；detached HEAD、错误 branch、无效 OID 和越出 common `worktrees` 的 gitdir 被拒绝；fast recover 测试确认不会启动 Git。

### 初始化、生命周期与删除保护

- create 从主 worktree 已提交 HEAD 建新 branch/worktree，拒绝 branch/path conflict，幂等写 common `info/exclude`，验证 linked branch/HEAD 后才持久化 record；
- 状态写失败会尝试 `git worktree remove --force` 和 branch 删除回滚；remove 失败会保留 record，不伪造成功；
- 本地文件和目录复制、已存在目标跳过、相对 hooksPath 转绝对并写 worktree config、dependency symlink、ignored 检查 0/1/错误和诊断持久化均有测试；
- 当前 Windows 不允许目录 symlink 时记录 `worktree_dependency_link_failed`，并确认目标不存在，没有回退为大目录复制；
- status 使用 porcelain `-z` 只计算条目数，不展示文件名；验证 clean、untracked dirty、无 upstream 新 commit、local bare remote upstream ahead 与 status failure；
- 普通 delete 只接受 clean/no-unpushed；dirty、unpushed、active、owner、branch 不匹配与检查失败均拒绝；`discard_confirmed=true` 仍重新读取 status，且不绕过 active/owner/branch/status 防护；
- manual worktree 永不自动清理；周期清理只处理状态登记、已过期、非 active、owner 不在用且实时安全的 worker；close 会 cancel/gather 清理 task 并幂等返回。

### Runtime、Worker、命令与恢复

- PathSandbox binding 把 working directory 和 roots 同时收窄到 worktree；并发 asyncio task 使用独立 ContextVar，退出后恢复默认 root；
- HookEngine 的 `project.root` 与 HookActionRunner shell cwd 使用相同绑定；异步 Hook task 继承创建时目录，Worker 收尾前按 worktree root drain；
- PromptRuntime fork 可替换 SessionEnvironment 与 Git request collector；worktree Worker 首个 session controls 同时写入 main root、isolated root、相对路径规则和禁止修改 main root 的边界；
- 定义式 `isolation: worktree` Worker 真实创建 `worker/<task_id>`，工具相对写入只落在 isolated root；clean 任务自动删除，dirty 任务保留并记录 `worktree_dirty`；主目录没有生成同名文件；
- worktree path、`preserved` 与稳定 reason 进入 Worker task snapshot、前台工具结果和后台 notification；Fork 继续不自动隔离，Hook recent 继续使用 Fork，none/summary 沿用 general 角色的 isolation；
- `/worktrees` 与 `/worktree create|enter|exit|status|delete` 全部本地执行；子命令、name 与 `--discard` 精确匹配；discard 在删除前展示 dirty count、unpushed 摘要和“不可恢复”并要求确认，manager 随后再次检查；
- enter/exit 写 active 状态并通过 `ChatApp.request_workspace_restart` 退出 TUI；CLI 只有在旧 runtime 的 Worker、Worktree、Notes、Hook、Session、MCP 与 artifact 都完成关闭后才以显式新 root 构造下一 runtime；
- console script 只接受零参数或精确 `--resume`；active=null 返回 main，active record 通过纯文件验证后恢复 isolated path，损坏状态拒绝启动，不扫描目录或回退猜测。

## 未连接或未执行的外部场景

本次默认验收没有调用 DeepSeek API，因为 `DEEPSEEK_API_KEY` 未设置；AgentLoop 与 Provider 行为使用本地确定性替身。没有连接公网、真实远端 Git remote、真实 MCP server 或插件；upstream ahead 使用 pytest 临时目录中的本地 bare repository 验证。

默认测试只在 pytest 临时 Git 仓库中创建和删除 linked worktree、branch 与本地 bare remote。没有对当前项目执行自动 push、commit、merge、reset、clean 或 worktree 删除。`uv build --wheel` 只构建本地产物，没有发布到外部 registry。

本次没有人工操作真实 Textual 终端完成 enter/exit；受控退出、restart target、资源关闭后重建、命令分发与 `--resume` 由确定性 App/UI/CLI 测试验证。POSIX 下的目录 symlink 成功路径与文件 symlink 越界路径未在当前 Windows 主机执行；相应拒绝逻辑和 Windows 失败不复制行为保留自动化覆盖。
