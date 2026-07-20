# Chapter 08 Evaluation：集中式斜杠命令与 UI 控制层

## 验收环境

| 字段 | 实际值 |
| --- | --- |
| 时间 | `2026-07-20T20:39:09.6541404+08:00` |
| 分支 | `master` |
| 验收代码基线 Commit | `3f5c753623da5f38ca418a3d19f3a8fa70cb7a38` |
| 真实 API 条件 | `DEEPSEEK_API_KEY` 未设置 |
| 代码状态 | README、CLI 注册目录断言与本验收记录尚未提交 |

## 自动化结果

| 检查 | 实际结果 |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -q -rs` | `685 passed, 4 skipped in 17.21s`，exit code `0` |
| 命令、App、CLI、会话、笔记、权限与上下文聚焦测试 | `139 passed in 13.13s`，exit code `0` |
| `.venv\Scripts\python.exe -m compileall -q src tests integration_tests` | exit code `0` |
| `git diff --check` | exit code `0`；只有 Git 的 LF→CRLF 工作区提示 |

四个跳过项分别是：

- `tests/test_instruction_loader.py::test_symlink_outside_root_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_notes_storage.py::test_project_notes_symlink_escape_is_rejected`：当前 Windows 环境不允许测试创建符号链接；
- `tests/test_security_boundary.py::test_path_sandbox_rejects_symlink_escape`：当前 Windows 环境不允许测试创建目录符号链接；
- `tests/test_session_storage.py::test_session_permissions_are_private_on_posix`：只适用于 POSIX 权限契约。

## 已验证场景

- 每条命令元数据包含规范名称、别名、描述、用法、执行类型、类别、参数提示、handler、隐藏标记和状态栏提示标记；字段类型、单行文本、usage 前缀和 identifier 均严格校验；
- name/name、name/alias、alias/name 和 alias/alias 冲突均在注册前原子拒绝；冻结后不能继续注册；
- 规范名称和别名通过同一个 key 空间解析，调用时只对命令名执行 `lower()`；参数大小写和内部 tab 等内容保持不变；
- parser 只在第一个 ASCII 空格切分，不使用 shell tokenizer，也不解释引号、反斜杠、环境变量或路径；
- 非斜杠输入返回 `consumed=False` 并进入 Agent；未知、空名称和非法 `/...` 输入被本地消费，不写历史、不调用 handler；
- 未知命令统一引导 `/help`；usage 错误、稳定领域错误和未预期异常均经过脱敏边界，异常正文不会进入 UI；
- 隐藏命令仍可精确 dispatch，但不出现在公开目录、帮助、状态栏提示或补全候选中；
- CLI 构造并冻结唯一内置 registry，公开规范命令顺序精确为 `help`、`status`、`mode`、`review`、`compact`、`clear`、`sessions`、`resume`、`session`、`memory`、`permissions`；
- `ChatApp.submit_prompt()` 不再引用 compact、session 或 notes 的独立 parser，也没有逐命令字符串分支；回车统一经过一个 controller worker；
- handler 依赖 `CommandUI` Protocol，不导入 Textual；Textual adapter 负责系统消息、确认、Agent user message、默认模式、 transcript 和状态栏；
- `local` 命令只读本地状态，`ui` 命令修改受限领域状态，`agent` 命令只能通过 UI adapter 把合成 user message送入现有 Agent run；
- `/help` 总览和单命令帮助均从 registry 元数据生成，alias 查询大小写不敏感，隐藏目标不会泄露；
- `/compact` 与 `/compress` 复用现有手动压缩事务，成功状态、参数拒绝和 Escape 取消均经过 TUI 测试；
- `/mode plan` 与 `/mode execute` 同步默认模式和 Switch；参数保持区分大小写，`/mode PLAN` 被拒绝；
- 状态栏始终包含 `mode=plan|execute` 和 registry 产生的 `/help /status /compact`，Agent、工具、压缩、恢复和错误状态更新不会丢失这些字段；
- Tab 单匹配直接补全；多匹配打开可键盘选择的 `OptionList` 弹窗；候选来自 registry，按规范名称和别名声明顺序生成；
- `/sessions`、`/resume`、`/session path` 和 `/session delete` 已迁移到集中 handler，session 子命令和 32 位小写十六进制 ID 仍严格校验；
- `/memory` 是规范笔记命令，`/notes` 是兼容别名；显示、路径和确认清空行为保持，通用确认请求包含精确 scope、session ID、标题和绝对路径；
- `/clear` 在真实 JSONL journal 上验证：旧 `messages.jsonl` 和 `meta.json` 逐字节不变，新 session 保持 lazy 且没有创建目录，内存历史清空；
- 新会话 activation 失败会恢复旧 session ID、journal 和历史，不创建或删除目标目录；会话仍没有按时间、数量或空间自动清理；
- 会话切换前会等待 note task，并对未处理成功请求尝试一次有界更新；切换后重置 note 计数与历史 cursor，manager 仍可继续触发更新；
- `/permissions` 显示配置/有效模式、覆盖状态、四层规则数量和三个配置路径；strict/default/permissive 覆盖只在当前进程内有效，reset 恢复配置值，测试确认不创建配置文件；
- permissive 进程覆盖只改变未命中规则的默认决策；显式项目规则和内置危险命令拒绝仍优先；
- `/status` 显示 provider、model、默认模式、session、历史消息、Prompt Token 估值与校准状态、预算、触发值、checkpoint、压缩熔断、笔记、权限和公开命令数量；
- Token 状态读取只调用无副作用估值，不执行工具结果外置、摘要、Provider 请求或历史修改；
- `/review` 和 `/code-review` 的默认及 scoped 模板逐字符验证；scope 原文保留，固定用 execute 运行但不改变默认模式；原始斜杠命令不持久化，只有合成 user message 进入历史；
- Chapter 01–07 的默认测试继续通过。

## 未连接的外部系统

本次默认验收没有调用 DeepSeek API，因为 `DEEPSEEK_API_KEY` 未设置。Agent、上下文估值、手动压缩、代码审查转发、确认界面和异步取消使用本地确定性测试替身验证。测试没有连接用户 MCP server，也没有读取或修改用户主目录中的真实命令、会话、笔记或安全配置。
