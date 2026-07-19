# Chapter 04：工具执行纵深防御

## 1. 目标

本章在 Chapter 02 的工具调度和 Chapter 03 的 Prompt 安全边界之下增加代码层纵深防御。所有工具调用在执行前统一得到 `allow`、`deny` 或 `ask` 决策；安全授权不依赖模型正文或控制标签。

## 2. 不可覆盖边界

以下检查先于所有配置规则、请求授权和权限模式：

1. 已知危险命令硬拒绝；
2. 文件工具目标路径必须位于应用启动工作目录；
3. `run_command.cwd` 必须位于应用启动工作目录；
4. 文件 glob 不能是绝对模式或包含独立的 `..` 路径段。

路径在检查前执行 `expanduser()` 和 `resolve(strict=False)`。因此已有符号链接或目录联接指向工作目录外时会被拒绝；新文件通过已存在父目录的规范化结果接受同一边界检查。Registry 在实际执行前再次运行不可覆盖边界，避免调用方绕过 Scheduler。

`run_command` 接收原始 PowerShell 或 `/bin/sh` 字符串。内置黑名单覆盖递归强制删除、磁盘操作、关机重启、远程下载后管道执行、破坏性 Git 清理和 fork bomb 已知形式。本章没有实现操作系统级进程沙箱，不承诺阻止所有字符串混淆、子进程文件访问、网络访问或未知危险程序。

## 3. 配置文件

用户全局文件精确为：

```text
Path.home() / ".mewcode-agent" / "security.yaml"
```

项目文件精确为：

```text
Path.cwd() / ".mewcode" / "security.yaml"
```

自动永久审批文件精确为：

```text
Path.home() / ".mewcode-agent" / "security-approvals.yaml"
```

用户文件根字段为 `version`、可选 `mode` 和 `rules`。项目文件根字段只能是 `version` 和 `rules`。三个文件都使用严格 YAML：拒绝重复键、未知字段、错误类型和重复规则 ID。配置不存在属于正常状态；已存在但无效时应用拒绝启动。

手工规则精确结构：

```yaml
id: command.allow_tests
action: allow
tool: run_command
priority: 100
match:
  command:
    kind: glob
    pattern: "uv run pytest*"
```

- `id` 完整匹配 `[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*`；
- `tool` 完整匹配 `[a-z][a-z0-9_]*`；
- `action` 只能为 `allow`、`deny`、`ask`；
- `priority` 必须为整数且不能是布尔值；
- `match` 必须为映射，可以为空；
- matcher 只能包含 `kind` 和 `pattern`；
- `kind` 只能为 `exact`、`glob`、`path_glob`；
- 同一规则不能重复匹配同一参数；
- 同一规则的全部 matcher 使用 AND 语义。

`exact` 比较值和精确类型；`glob` 对完整字符串执行大小写敏感 glob；`path_glob` 只作用于内置工具的 `path` 或 `cwd` 参数，并对路径沙箱根目录下的 POSIX 风格相对路径逐段执行 glob，其中 `**` 匹配零个或多个完整路径段。

## 4. 决策顺序

固定顺序如下：

1. 危险命令硬拒绝；
2. 路径沙箱；
3. 会话临时规则；
4. 项目规则；
5. 用户全局规则和永久审批；
6. 当前 request 的计划批准；
7. 权限模式默认决策。

某一规则层有匹配后不检查更低规则层。同一层先选择最大 `priority`，再按 `deny > ask > allow`，最后按 `id` 字典序选择第一条。

计划批准只允许没有命中具体规则的 write 或 command 调用，不能覆盖明确的 `deny`、`ask`、硬拒绝或路径沙箱。

## 5. 权限模式

| mode | read | write | command |
| --- | --- | --- | --- |
| `strict` | ask | ask | ask |
| `default` | allow | ask | ask |
| `permissive` | allow | allow | allow |

模式只提供未匹配调用的默认决策。项目配置不能声明 `mode`。

## 6. HITL 生命周期

- `allow_once`：只执行当前调用，不生成规则；
- `allow_session`：生成当前进程内的会话规则；
- `allow_permanent`：原子写入用户永久审批文件；
- `reject`：当前调用返回结构化拒绝结果。

自动审批规则使用 SHA-256 安全指纹。write/edit 指纹包含规范化工具路径但不包含文件正文或编辑正文；command 指纹对精确命令和 cwd 计算哈希；所有永久规则绑定精确项目根目录。永久审批文件不保存原始命令、文件内容、编辑内容或 API Key。

## 7. 执行接入

`ToolScheduler` 在发出 `ToolCallStartedEvent` 前评估安全策略。`deny` 不产生 started 事件；`ask` 先产生 `ToolApprovalRequestedEvent` 并等待 `AgentRunContext`；只有最终允许才执行 Registry。连续 read 调用仍并发执行，审批在 started 事件之前按原调用顺序处理，结果继续保持原顺序。

`ToolRegistry` 对已解析参数再次执行危险命令和路径边界检查。文件工具本身使用同一个 `PathSandbox` 解析目标路径，形成 Scheduler、Registry、工具实现三层检查。
