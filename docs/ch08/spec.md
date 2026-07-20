# Chapter 08 Specification：集中式斜杠命令与 UI 控制层

## 1. 范围与基线

- 前置实现：Chapter 03 Prompt 运行时、Chapter 04 工具安全、Chapter 06 上下文压缩、Chapter 07 会话与笔记。
- 本章目标：建立唯一的斜杠命令注册、解析和分发入口，把命令处理从 Textual 组件中拆出，并提供帮助、补全、模式、状态和 AI 工作流命令。
- 本章只注册应用内置命令，不加载项目或用户提供的可执行命令代码。

## 2. 已确认决策

1. 输入去除首尾空白后以 `/` 开头时，一律进入命令分流，不直接发送给模型。
2. 命令名是 `/` 后至第一个 ASCII 空格 `U+0020` 前的字符串。
3. 命令名调用 `lower()` 后解析，因此命令名和别名大小写不敏感。
4. 参数不做大小写转换、路径转换、相似匹配或标识符修复。
5. 未知命令被本地消费，统一提示使用 `/help`，不进入普通历史、JSONL 或 Prompt request sequence。
6. 所有命令由同一个 `CommandRegistry` 注册；注册时检测名称和别名的规范化冲突。
7. 命令按 `local`、`ui`、`agent` 三种执行类型分类。
8. 命令处理函数只依赖 `CommandUI` 与显式服务接口，不导入 Textual widget、screen 或 worker。
9. `/clear` 不删除旧会话：它保留当前会话存档并切换到新的 lazy 空会话。
10. 会话仍不执行任何自动清理。
11. `/permissions` 的模式覆盖只在当前应用进程内有效，不修改任何 YAML 或永久审批文件。
12. `/review` 把代码审查预设提示作为真实 user 消息送入 Agent；原始 `/review` 命令文本本身不写入历史。
13. 隐藏命令可以精确调用，但不出现在帮助、状态栏提示或 Tab 补全中。

## 3. 与旧章节的兼容性变化

Chapter 06 和 Chapter 07 原先只识别大小写精确的命令，非精确形式按普通用户消息处理。本章实施后由以下新规则替代：

- `/COMPACT` 与 `/compact` 解析为同一命令；
- `/NOTES paths` 通过 `notes` 别名解析为 `/memory paths`；
- 未知 `/name` 不再作为普通用户消息发送给模型；
- 参数仍保持区分大小写，例如 `/mode PLAN` 是参数错误，不等同于 `/mode plan`；
- 旧的 session ID 仍只接受精确 32 位小写十六进制字符串，不转换大写 ID。

## 4. 模块边界

新增模块：

```text
src/mewcode_agent/commands/
├── __init__.py
├── models.py
├── registry.py
├── parser.py
├── controller.py
└── builtins.py
```

| 模块 | 职责 |
| --- | --- |
| `commands.models` | 命令元数据、调用、结果、错误、状态与 UI 协议 |
| `commands.registry` | 注册、冲突检查、精确查找、帮助目录和补全候选 |
| `commands.parser` | `/` 前缀识别、命令名切分和小写规范化 |
| `commands.controller` | 未知命令处理、handler 调用、异常边界和 consumed 结果 |
| `commands.builtins` | 内置命令声明、参数验证和 UI/服务调用 |

现有模块调整：

| 模块 | 调整 |
| --- | --- |
| `app.py` | 实现 Textual `CommandUI` adapter，只保留输入分流、worker 与渲染 |
| `sessions.manager` | 提供显式创建新 lazy session 的事务，不再解析命令文本 |
| `notes.manager` | 提供会话切换边界与状态快照，不再解析命令文本 |
| `security.policy` | 提供当前进程权限模式覆盖与脱敏状态快照 |
| `compaction.manager` | 提供不修改历史的 Prompt 估值状态快照 |
| `agent.loop` | 汇总上下文状态，并继续承载 Agent run 与压缩事务 |
| `cli.py` | 构造命令注册中心和 controller，显式注入服务依赖 |

## 5. 命令模型

### 5.1 标识符

命令名称和别名必须完整匹配：

```regex
[a-z][a-z0-9-]*|\?
```

规则：

1. 注册输入必须已经是小写；注册中心不静默修复元数据。
2. `?` 只允许作为完整别名，用于 `/?`。
3. 名称和别名均不包含 `/`、空白、下划线、点或非 ASCII 字符。
4. 帮助查询和调用时只对输入的命令名执行 `lower()`。

### 5.2 `CommandSpec`

每条命令固定包含：

```text
name
aliases
description
usage
execution_kind
category
argument_hint
handler
hidden
status_hint
```

约束：

- `name` 是唯一规范名称；
- `aliases` 是保持声明顺序的 tuple，不能与本条 name 或其他 alias 重复；
- `description` 和 `usage` 必须为非空单行字符串；
- `usage` 必须以规范 `/<name>` 开头；
- `execution_kind` 只能为 `local`、`ui`、`agent`；
- `category` 只能为 `general`、`workflow`、`context`、`sessions`、`memory` 或 `security`；帮助按该声明顺序分组；
- `argument_hint` 可以为空；非空时必须是单行字符串；
- `handler` 是异步 callable；
- `hidden` 和 `status_hint` 必须是 bool；隐藏命令不能同时标记为状态栏提示。

### 5.3 `CommandInvocation`

成功解析后保存：

```text
spec
invoked_name
arguments
```

- `spec` 是解析到的规范元数据；
- `invoked_name` 是 lower 后的实际名称或别名；
- `arguments` 是第一个 ASCII 空格之后去除首尾 ASCII 空格的剩余正文；内部字符逐字符保留。

命令 handler 不接收原始整行，从而不能误把斜杠命令写入普通历史。

## 6. 注册中心

### 6.1 注册事务

`CommandRegistry.register(spec)` 必须在修改任何映射前验证完整 spec。

所有 name 和 alias 放入同一个规范化 key 空间。以下情况产生 `CommandRegistrationError` 且注册中心零修改：

- 新 name 已被其他 name 使用；
- 新 name 已被其他 alias 使用；
- 任一新 alias 已被其他 name 或 alias 使用；
- 本条 spec 内 name/alias 重复；
- 元数据类型或格式无效。

错误只报告冲突 key，不包含 handler repr、参数内容或配置正文。

### 6.2 查找与目录

- `resolve(name)` 只做 `lower()` 后的精确 key 查找，不做前缀、编辑距离或模糊匹配；
- `public_specs()` 排除 `hidden=True`，按 `category`、注册顺序返回；
- help 中每条规范命令只展示一次，别名作为元数据附在同一行；
- registry 构造完成后由 CLI 调用 `freeze()`；冻结后再注册产生稳定错误，防止运行中帮助与补全列表变化。

### 6.3 补全候选

`completion_candidates(prefix)`：

1. prefix 不含 `/`，调用 `lower()`；
2. 返回匹配前缀的公开规范名称和公开别名；
3. 隐藏命令的 name 与 aliases 全部排除；
4. 候选按规范命令注册顺序、name 在 aliases 前、alias 声明顺序排列；
5. 不去猜测拼写，不返回不匹配的近似名称。

## 7. 解析与分发

### 7.1 输入分类

UI 首先对提交值调用一次 `strip()`：

- 结果为空：忽略；
- 首字符不是 `/`：返回 `not_command`，由 UI 发送给 Agent；
- 首字符是 `/`：返回命令候选，并且无论成功与否都由命令系统消费。

### 7.2 切分

对 `/` 前缀后的正文：

1. 在第一个 ASCII 空格切分一次；
2. 左侧为空时是未知命令；
3. 左侧包含控制字符或不满足命令调用标识符时是未知命令；
4. 左侧调用 `lower()`；
5. 右侧只调用 `strip(" ")`，不改变 tab、大小写、斜杠、引号或路径分隔符。

解析器不使用 shell tokenizer，不解释引号、反斜杠、环境变量或转义序列。各 handler 对自己的参数执行精确验证。

### 7.3 Controller

`CommandController.dispatch(text)` 返回：

```text
consumed
command_name
execution_kind
success
```

- 非命令只返回 `consumed=False`，不调用 UI；
- 未知命令返回 `consumed=True`，调用 `show_system_message()` 输出统一帮助提示；
- 已知命令调用一次 handler；
- `CommandUsageError` 显示该命令的精确 usage；
- `CommandError` 只显示稳定 code 和固定消息；
- 未预期异常显示 `command_failed`，不得包含 exception 文本；
- 命令输出、usage 错误和未知命令都不进入普通历史。

未知命令固定输出：

```text
未知命令：/<lower_name>。输入 /help 查看可用命令。
```

空名称 `/` 固定输出：

```text
未知命令。输入 /help 查看可用命令。
```

## 8. 执行类型

### 8.1 `local`

- 只读取已有状态并显示本地结果；
- 不修改历史、会话、模式、权限或文件；
- 不调用 Provider。

本章的 `help`、`status` 属于此类。

### 8.2 `ui`

- 修改应用或领域状态；
- 可以执行本地异步 I/O 和请求确认；
- 不调用普通 Agent run；
- 除显式会话/笔记文件事务外不写入对话历史。

本章的 `compact`、`clear`、`mode`、`sessions`、`resume`、`session`、`memory` 和 `permissions` 属于此类。

### 8.3 `agent`

- handler 生成固定模板的 user message；
- 通过 `CommandUI.send_user_message()` 进入现有 Agent 事件流；
- 生成的 user message 写入历史与 JSONL；
- 原始斜杠命令不写入历史；
- 不允许 handler 直接调用 Provider 或 ToolScheduler。

本章的 `review` 属于此类。

## 9. UI 控制接口

`CommandUI` 是不引用 Textual 的 Protocol，包含以下能力：

```text
show_system_message(lines)
request_confirmation(request)
send_user_message(message, mode)
get_default_mode()
set_default_mode(mode)
clear_transcript()
refresh_status(state)
```

规则：

1. `show_system_message` 的内容只进本地 command output，不进历史。
2. `request_confirmation` 使用不可变 `ConfirmationRequest`，字段值由 handler 从已验证的领域对象取得。
3. `send_user_message` 是 agent 命令进入 Agent 的唯一入口。
4. `mode` 只能为 `plan` 或 `execute`；转换到 `AgentLoop.run(plan_only=...)` 只在 adapter 内完成。
5. handler 不查询 widget ID，不设置 Input/Switch 属性，也不创建 Worker。
6. Textual adapter 负责在 dispatch 期间禁用输入和模式控件，完成后恢复并聚焦输入框。

会话、笔记、压缩、安全和状态读取通过 `CommandServices` 显式注入，不塞入全局变量，也不从 UI widget 反向取得。

## 10. 内置命令目录

| 规范命令 | 别名 | 类型 | 用法 |
| --- | --- | --- | --- |
| `help` | `h`, `?` | local | `/help [command]` |
| `compact` | `compress` | ui | `/compact` |
| `clear` | `new` | ui | `/clear` |
| `mode` | 无 | ui | `/mode [plan\|execute]` |
| `sessions` | 无 | ui | `/sessions` |
| `resume` | 无 | ui | `/resume <session_id>` |
| `session` | 无 | ui | `/session <path\|delete> <session_id>` |
| `memory` | `notes` | ui | `/memory [paths\|clear user\|clear project]` |
| `permissions` | `perms` | ui | `/permissions [strict\|default\|permissive\|reset]` |
| `status` | `stat` | local | `/status` |
| `review` | `code-review` | agent | `/review [scope]` |

只有 `help`、`status` 和 `compact` 的 `status_hint=True`，其余内置命令均为 `False`。

## 11. 内置命令行为

### 11.1 帮助

`/help` 按 category 与注册顺序显示全部公开规范命令，每条包含 name、aliases、description 和 usage。

`/help <command>`：

- 参数可以带或不带一个开头 `/`；
- 对名称调用 `lower()` 后精确查找；
- 显示 description、execution kind、usage、aliases 和 argument hint；
- 不接受空白分隔的其他参数；
- 查询隐藏命令按未知帮助目标处理。

### 11.2 上下文压缩

`/compact` 和 `/compress` 不接受参数，调用现有手动压缩事务。取消、失败、无可压缩历史和成功结果沿用 Chapter 06 的稳定状态。

### 11.3 清空对话

`/clear` 和 `/new` 不接受参数，精确执行：

1. 确认当前没有 Agent、命令或摘要任务运行；
2. 等待已经运行的 note task；存在尚未处理的成功请求时按退出时的 `120` 秒边界再尝试一次更新，失败只产生脱敏 warning，不阻止切换；
3. 关闭当前 session journal；
4. 保留当前 session 目录、JSONL、meta 和 context artifact，不删除任何文件；
5. 生成新的 32 位小写十六进制 session ID，但保持 lazy，不立即建目录；
6. 把内存普通历史替换为空；
7. 重新加载当前指令和笔记 session controls；
8. 重置 Prompt request/round timeline、checkpoint、估值基线和压缩熔断；
9. 清空当前 transcript 与 command output；
10. 状态栏显示新的 session ID 与 `就绪`。

旧会话仍可通过 `/sessions` 和 `/resume` 找回。该命令不等同于删除、自动清理或清空笔记。

### 11.4 默认模式

- `/mode` 显示当前默认模式；
- `/mode plan` 把后续普通用户消息的默认值设为 plan，并同步 Switch；
- `/mode execute` 把默认值设为 execute，并同步 Switch；
- 参数只接受精确小写 `plan` 或 `execute`；
- 模式只存在于当前 Textual 进程，不写入会话、笔记或配置文件；
- 恢复或新建会话不改变当前默认模式。

### 11.5 会话

会话 handler 迁移 Chapter 07 的现有行为：

- `/sessions`：列出当前项目 meta；
- `/resume <session_id>`：恢复会话；
- `/session path <session_id>`：显示精确绝对路径；
- `/session delete <session_id>`：确认后删除非活动会话。

命令名大小写不敏感；子命令和 session ID 参数仍精确、区分大小写。删除安全边界和“不自动清理”约束不变。

### 11.6 记忆

`memory` 是规范名称，`notes` 是兼容别名：

- `/memory`、`/notes`：显示四类当前笔记；
- `/memory paths`、`/notes paths`：显示两个精确绝对路径；
- `/memory clear user`、`/notes clear user`：确认后清空用户级笔记；
- `/memory clear project`、`/notes clear project`：确认后清空项目级笔记。

子命令和 scope 参数保持精确小写。

### 11.7 权限

`/permissions` 和 `/perms` 显示脱敏状态：

- 配置模式；
- 当前有效模式；
- 是否存在进程内覆盖；
- user/project/permanent/session 规则数量；
- 用户级、项目级和永久审批配置的精确绝对路径。

`/permissions strict|default|permissive` 设置当前进程覆盖，`/permissions reset` 恢复启动配置模式。

进程内覆盖只改变未命中规则调用的默认处理。内置拒绝、路径沙箱、项目/user 规则、永久规则和会话审批继续按原优先级执行，不能被 `permissive` 绕过。命令不写 YAML，不创建永久审批。

### 11.8 综合状态

`/status` 不接受参数，显示：

- provider ID 和 model；
- 当前默认模式；
- 当前 session ID；
- 普通历史消息数；
- 当前 Prompt 估算 Token、估值是否由真实 usage 校准、Prompt budget、自动压缩触发值；
- checkpoint generation 和覆盖消息数；
- 自动压缩熔断状态与连续失败次数；
- 笔记 generation 和未处理成功请求数；
- 当前有效权限模式；
- 已注册公开命令数。

Token 状态读取只组装并估算当前 Prompt，不外置工具结果、不发起摘要、不调用 Provider、不修改 estimator 基线或历史。

### 11.9 代码审查

`/review` 生成以下固定 user message：

```text
请审查当前工作区尚未提交的代码更改。只读取和分析，不修改文件。请按严重程度列出可复现的问题，并给出精确文件与行号；如果没有发现问题，请明确说明剩余测试风险。
```

`/review <scope>` 生成：

```text
请审查以下用户指定范围内的代码。只读取和分析，不修改文件。
用户指定范围（原文）：
<scope>
请按严重程度列出可复现的问题，并给出精确文件与行号；如果没有发现问题，请明确说明剩余测试风险。
```

scope 使用 parser 产生的参数原文，不解析路径、不检查存在性、不改变大小写。

审查请求固定用 `execute` 运行，使 Agent 可以直接返回审查结果，不弹出计划批准；它不改变 UI 的默认模式。工具仍经过完整安全策略，预设提示本身不授予写入或命令权限。

## 12. Textual 输入分流

回车事件顺序固定为：

```text
读取并清空 Input
→ 空输入直接返回
→ CommandController.dispatch()
→ consumed=True：等待命令完成并恢复控件
→ consumed=False：按当前默认模式启动 Agent run
```

禁止在 `submit_prompt()` 中继续出现 `/compact`、session 或 notes 的独立字符串判断。所有内置斜杠命令必须通过 registry handler 到达。

同一时刻只允许一个 Agent run 或一个命令 worker。输入 disabled 时不接受新的 dispatch；Escape 继续取消活动 Agent run 或可取消的手动压缩。普通本地命令不得取消已有任务。

## 13. Tab 补全

### 13.1 触发条件

按 Tab 时只有同时满足以下条件才处理：

- Input 未 disabled；
- 值去除开头空白后以 `/` 开头；
- 光标位于输入末尾；
- `/` 后到光标之间不包含 ASCII 空格；
- 当前没有打开确认或补全界面。

否则 Tab 保持 Textual 默认焦点行为。

### 13.2 行为

- 无匹配：不改输入，状态提示 `没有匹配的命令；输入 /help 查看帮助`；
- 一个匹配：替换为 `/<candidate> `，把光标置于末尾；
- 多个匹配：打开命令补全列表，只显示 registry 返回的公开候选；
- 列表支持 Up、Down、Enter 和 Escape；Enter 选择后写入 `/<candidate> `；Escape 关闭且不改输入；
- 输入变化或失焦关闭旧列表；
- 不做模糊补全，不补全参数，不展示隐藏命令。

用户输入中的大写 prefix 可以匹配小写候选；完成结果使用注册的精确小写名称或别名。

## 14. 状态栏

空闲状态固定包含：

```text
<provider_id> | <model> | mode=<plan|execute> | 就绪 | /help /status /compact
```

运行状态继续显示生成、规划、执行、工具、压缩、恢复或错误信息，但始终保留 `mode=<plan|execute>`。Switch 改变、`/mode`、`/clear` 和 `/resume` 后立即刷新。

高频命令提示来自 registry 中显式标记的三个固定公开命令，不在 UI 中复制命令字符串。若构造时缺少任一提示命令，CLI 启动失败并产生 `command_registry_invalid`。

## 15. 会话与笔记切换边界

`/clear` 与 `/resume` 都必须使用同一会话激活服务：

1. 等待当前 note task；
2. 存在尚未处理的成功请求时，在 `120` 秒边界内尝试一次 note update；失败只发 warning；
3. 切换或创建 session journal；
4. 重新读取当前磁盘上的指令和笔记；
5. 重置 note manager 的本会话计数与历史 cursor；
6. 重置 AgentLoop session 状态；
7. 重新外置恢复出的超大工具结果；
8. 仅恢复会话执行超预算检查与七天提醒；新空会话不执行；
9. 任一步失败时恢复原 journal、历史和运行时状态，或返回稳定 `session_switch_failed`。

不得因 `/clear`、`/resume`、应用启动或退出删除其他会话。显式 `/session delete` 是唯一删除入口。

## 16. 错误与脱敏

| 错误码 | 含义 |
| --- | --- |
| `command_registry_invalid` | 元数据无效、冲突或冻结状态错误 |
| `command_usage_invalid` | 已知命令参数不符合精确 usage |
| `command_unavailable` | 当前构造未提供命令所需服务 |
| `command_failed` | 未预期 handler 或 UI adapter 失败 |
| `command_confirmation_failed` | 确认界面未能完成 |
| `command_status_failed` | 状态读取或 Token 估值失败 |
| `session_switch_failed` | 新建或恢复会话激活事务失败 |

错误 UI 不显示 exception repr、Provider request、Prompt、用户参数之外的文件正文、安全 matcher、API Key、MCP env/header、笔记正文或 JSONL 内容。

用户参数属于用户已经输入的内容，可以在 usage 错误中省略，但不得被拼接进未预期异常文本。

## 17. 安全约束

1. 只有用户在 Input 中提交的文本能触发命令；模型输出、工具结果、恢复历史、笔记和项目指令中的 `/...` 文本不能触发 dispatch。
2. agent 类型 handler 只能调用 `CommandUI.send_user_message`，不能直接调用 Provider 或 scheduler。
3. local/ui 命令不能把输出加入历史伪装为 user、assistant 或 tool 消息。
4. `/review` 的只读要求不替代安全策略；所有工具仍经过 scheduler 和 policy engine。
5. `/permissions permissive` 不能绕过代码层拒绝、PathSandbox 或已配置规则。
6. `/clear` 不删除 session、note、artifact 或配置文件。
7. session delete 和 note clear 仍要求确认，不能因 alias 或大小写不敏感而跳过。
8. help、completion 和 status 不显示隐藏命令、handler 信息或安全规则正文。
9. registry 不根据相似名称调用命令；未知命令只引导 help。

## 18. 非目标

1. 从项目文件、Markdown、YAML、Python entry point 或 MCP 动态加载命令。
2. Shell 风格参数解析、管道、重定向、变量替换或命令串联。
3. 模糊搜索、拼写纠正或自动执行最接近命令。
4. 参数级 Tab 补全、文件路径补全或 session ID 补全。
5. 把命令输出持久化到 JSONL。
6. 允许模型或工具触发本地命令。
7. 永久保存 UI 默认 plan/execute 模式。
8. 通过 `/permissions` 编辑、删除或新增安全规则和永久审批。
9. `/clear all`、批量删除会话或任何自动清理。
10. 通用宏、用户自定义 prompt 命令或插件命令市场。

## 19. 测试策略

### 19.1 模型与注册

- 所有字段类型、单行要求和 identifier 边界；
- name/name、name/alias、alias/name 和 alias/alias 冲突全部原子拒绝；
- 同一 spec 内重复拒绝；
- freeze 后注册拒绝；
- hidden 从 help 与 completion 排除但可精确 dispatch；
- public 顺序和别名顺序稳定。

### 19.2 解析与分发

- 非 `/` 输入不消费；
- `/`、未知命令和非法 command token 本地消费；
- 命令名大小写不敏感；
- 参数大小写和内部内容逐字符保留；
- 只在第一个 ASCII 空格切分；
- handler 一次调用、usage 错误和未预期错误脱敏；
- 未知命令不进入历史、不递增 request sequence。

### 19.3 UI adapter

- 普通输入只进入 Agent；命令输入只进入 controller；
- dispatch 期间控件 disable/focus 恢复；
- agent 命令只持久化合成 user message；
- 命令输出不持久化；
- Tab 零、一、多候选和 hidden 排除；
- 补全列表键盘选择和取消；
- 状态栏模式与提示随状态稳定存在。

### 19.4 内置命令

- help 总览、单命令、alias 查询和 hidden 拒绝；
- compact alias 与参数拒绝；
- clear 保留旧会话逐字节内容并创建 lazy 新 session；
- mode 查询、切换、参数大小写拒绝和 Switch 同步；
- 现有 session 与 notes 行为通过集中 handler 保持；
- permission 覆盖、reset、规则优先级和不落盘；
- status Token 估值只读且字段完整；
- review 两种模板逐字符匹配、scope 原文保留、固定 execute 且不改变默认模式。

### 19.5 回归

- Chapter 01–07 默认测试全部通过；
- 会话仍不自动清理；
- Prompt 用户原话、上下文压缩和自动笔记契约不变；
- 工具审批与计划审批不能被命令层绕过；
- 默认测试不访问公网、不需要真实 Provider 或 MCP server。

## 20. 验收标准

1. 所有斜杠命令由一个冻结注册中心声明和解析，App 不再包含逐命令字符串分支。
2. 名称与别名冲突在注册时原子拒绝。
3. 命令名大小写不敏感，参数不被大小写或格式转换。
4. 未知斜杠命令只引导 `/help`，不发送给 AI。
5. local、ui、agent 三类 handler 的历史、Provider 和状态修改边界可测试。
6. 命令 handler 不依赖 Textual 类型或 widget ID。
7. 帮助和 Tab 补全从 registry 元数据生成，隐藏命令不参与。
8. 状态栏持续显示当前模式和三个高频命令提示。
9. help、compact、clear、mode、sessions、resume、session、memory、permissions、status 和 review 均通过注册中心工作。
10. `/clear` 只创建新的 lazy 会话，不删除或改写旧会话存档。
11. `/permissions` 覆盖只在当前进程生效，安全边界和显式规则仍优先。
12. `/status` 提供只读 Token 估值，不触发 Provider、摘要或历史修改。
13. `/review` 只把固定合成 prompt 写入历史，原始斜杠文本不进入历史。
14. session delete 与 note clear 保持确认流程。
15. 会话没有任何自动清理路径。
16. Chapter 01–07 全量回归通过。
