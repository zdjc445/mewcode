# Chapter 07 Evaluation：项目指令、会话存档与分层自动笔记

## 验收环境

| 字段 | 实际值 |
| --- | --- |
| 时间 | `2026-07-20T12:38:25.7287875+08:00` |
| 分支 | `master` |
| 验收代码基线 Commit | `41a30c8a1129d2faa9d24a48769e14ea5c903438` |
| 真实 API 条件 | `DEEPSEEK_API_KEY` 未设置 |
| 代码状态 | README、最终生命周期测试与本验收记录尚未提交 |

## 自动化结果

| 检查 | 实际结果 |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -q -rs` | `657 passed, 4 skipped in 16.21s`，exit code `0` |
| 指令、会话、笔记与 CLI 聚焦测试 | `112 passed, 2 skipped in 7.74s`，exit code `0` |
| `.venv\Scripts\python.exe -m compileall -q src tests integration_tests` | exit code `0` |
| `git diff --check` | exit code `0`；只有 Git 的 LF→CRLF 工作区提示 |

四个跳过项分别是：

- `tests/test_instruction_loader.py::test_symlink_outside_root_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_notes_storage.py::test_project_notes_symlink_escape_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_security_boundary.py::test_path_sandbox_rejects_symlink_escape`：当前 Windows 环境不允许测试创建目录符号链接；
- `tests/test_session_storage.py::test_session_permissions_are_private_on_posix`：只适用于 POSIX 权限契约。

## 已验证场景

- 项目级 `MEWCODE.md` 严格排在用户级 `INSTRUCTIONS.md` 之前；缺失和空文件不会生成控制消息；
- `@include` 在原位置展开，只接受所属根目录内的精确相对路径，并拒绝绝对路径、`..` 越界、符号链接越界、循环、缺失目标、目录目标和第 `6` 层嵌套；
- 指令文件使用严格 UTF-8、`64 KiB` 单文件上限和 `256 KiB` 单层展开上限；错误只暴露稳定错误码，不包含文件正文；
- 每条 user、assistant 和 tool 消息先追加并 fsync 到 JSONL，再进入内存历史；meta 使用原子替换，空会话不创建目录，持久化失败不推进内存历史；
- JSONL 使用固定字段顺序和单个 LF，拒绝重复键、错误字段顺序和超过 `65 MiB` 的单行；工具结果的内存 preview 替换不写回存档；
- 恢复会跳过 UTF-8、JSON、schema、超大行和 sequence 回退错误，保留坏行之后仍独立有效的记录，并规范化 sequence、meta 和结尾换行；
- 未闭合、重复、未知或乱序的工具调用批次会截断到最后完整边界，修复后的会话能够继续追加；
- 会话列表只读取有效 `meta.json`，只返回精确项目路径匹配的会话，并按更新时间和 session ID 稳定排序；
- `/resume` 成功后切换历史与 journal 并延续 sequence；激活失败会恢复原 journal 和内存历史；七天边界的时间跨度提醒经过测试；
- 会话启动为惰性，不创建空目录；管理器启动和 CLI 完整启动/退出都不会清理既有会话。CLI 生命周期测试使用更新时间为 `2000-01-01T00:00:00+00:00` 的有效会话，退出后 JSONL 与 meta 逐字节不变；
- 会话没有按年龄、数量或磁盘空间进行自动清理；删除只存在于精确 `/session delete <session_id>`、非活动目标和确认界面共同满足的路径；
- 两层笔记 Markdown 严格解析并稳定生成；单文件上限为 `256 KiB`，项目笔记符号链接越界拒绝，原子写入失败保留旧文件；
- 自动笔记只统计成功的 FinalResponse，每 `5` 次触发；并发阈值合并且同一时刻只有一个任务，失败后需再有 `5` 次新成功才重试；
- 退出时无新增不会请求，存在未处理成功请求时更新一次，超过 `120` 秒会取消并产生脱敏 warning；
- 笔记更新使用当前 Provider 且 `tools=None`，Prompt 首尾禁止工具；工具调用、事件乱序、usage 缺失、错误 stop reason、超大正文和错误 JSON 均被拒绝；
- 最近历史按最多 `12` 个完整原子单元取样，超过 `512 KiB` 时只丢弃最旧完整单元，不拆分工具批次，也不截断已有笔记；
- `analysis_draft` 解析后丢弃，四类 notes 键和顺序严格校验；用户偏好/纠正反馈与项目知识/参考资料分别写入用户级和项目级文件；
- 新笔记 generation 替换同 scope 的旧 Prompt 投影；笔记以 `context` 注入，不作为授权、指令或文件事实；
- `/sessions`、`/resume`、`/session path`、`/session delete`、`/notes`、`/notes paths`、`/notes clear user` 和 `/notes clear project` 的精确命令均不进入普通历史；非精确形式按普通用户消息处理；
- CLI 启动顺序包含指令和笔记加载；退出顺序经过测试，固定为 notes flush、session close、MCP close、artifact close；
- Chapter 01–06 的默认测试继续通过。

## 未连接的外部系统

本次默认验收没有调用 DeepSeek API，因为 `DEEPSEEK_API_KEY` 未设置。笔记更新的 Provider 流、usage、工具调用拒绝、事件错误、超时和取消路径使用本地确定性测试替身验证。测试没有连接用户 MCP server，也没有读取或修改用户主目录中的真实指令、会话或笔记。
