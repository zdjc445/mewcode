# Chapter 04 Evaluation：工具执行纵深防御

## 验收环境

| 字段 | 实际值 |
| --- | --- |
| 时间 | `2026-07-19T20:12:55+08:00` |
| 分支 | `master` |
| 验收基线 Commit | `d93523c93960d177ae09ceac06232dafe9873d6b` |
| 代码状态 | Chapter 04 改动尚未提交 |

## 自动化结果

| 检查 | 实际结果 |
| --- | --- |
| `uv run pytest -q` | `338 passed, 1 skipped in 9.87s`，exit code `0` |
| `uv run pytest -q -rs tests/test_security_boundary.py` | `14 passed, 1 skipped in 0.10s`，exit code `0` |
| `uv run python -m compileall -q src tests integration_tests` | exit code `0` |
| `git diff --check` | exit code `0`；只有 Git 的 LF→CRLF 工作区提示 |
| 新增/修改 Python 与 Markdown 尾随空白扫描 | 无匹配，exit code `0` |
| 符号链接越界实测 | 未执行：当前 Windows 环境不允许创建目录符号链接 |

跳过项来自 `tests/test_security_boundary.py::test_path_sandbox_rejects_symlink_escape`。生产实现使用 `Path.resolve(strict=False)` 后再做规范化根目录包含检查；本页不把未能创建真实符号链接的测试记为本机实测通过。

## 已验证场景

- 硬拒绝优先于 permissive、配置 allow 和当前 request 授权；
- 文件路径、`run_command.cwd` 和文件 glob 越界被拒绝；
- Registry 直接调用仍重复检查路径和危险命令；
- session、project、user 层级和同层稳定排序符合规格；
- strict、default、permissive 的 read/write/command 默认决策符合规格；
- allow-once 不延续，allow-session 在同一策略引擎中复用；
- 永久审批跨策略引擎重载生效并绑定精确项目根目录；
- 永久审批文件不包含原始命令或文件正文；
- 永久审批写入失败时工具不执行；
- TUI 四种审批选择和取消路径通过回归；
- Chapter 01–03 默认测试继续通过。

## 明确未提供的保证

当前实现不是操作系统级进程沙箱。`run_command` 子进程仍可能通过未被黑名单识别的程序、混淆形式或子进程访问工作目录外资源和网络。准确边界为：内置文件工具路径沙箱、命令工作目录限制、已知危险命令硬拒绝、分层规则和 HITL；不包含内核级文件系统、网络或进程隔离。
