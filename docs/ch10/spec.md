# Chapter 10 Specification：声明式 Hook 规则引擎

## 1. 范围与基线

- 前置实现：Chapter 03 Prompt 运行时、Chapter 04 工具安全与审批、Chapter 06 上下文压缩、Chapter 07 会话边界、Chapter 08 命令中心、Chapter 09 Skill。
- 本章目标：在 Agent Loop 的稳定生命周期边界发出结构化事件，以严格 YAML 规则匹配上下文并执行 shell、Prompt 注入、HTTP 或预留 subagent 动作。
- Hook 配置是用户主动放置的受信任自动化配置；本章不提供在线下载、图形化编辑器或隐式配置迁移。
- `subagent` 动作在本章完成配置、匹配、超时和诊断契约，但执行器保持显式不可用；Chapter 11 接入统一子工作者后替换该占位实现。

## 2. 已确认决策

1. 用户级配置固定为 `~/.mewcode-agent/hooks.yaml`，项目级配置固定为 `<project_root>/.mewcode/hooks.yaml`。
2. 两层文件都使用 UTF-8 严格 YAML；缺失文件表示该层没有规则，不自动创建文件。
3. 项目级规则优先于用户级规则；同一精确 `id` 的项目规则完整覆盖用户规则，不做字段合并。
4. 生效规则先按来源优先级排列，再保持所在文件中的声明顺序。
5. 规则由精确事件名、`match` 条件、单个 `action`、执行方式、超时和可选工具拦截组成。
6. 条件沿用权限规则的 `{kind, pattern}` 形态，并支持 `exact`、`glob`、`regex` 与递归 `not`。
7. 模板占位符固定使用 `${context.path}`；未知字段是该次动作错误，不替换为空字符串，也不回显敏感上下文。
8. `once: true` 表示当前应用进程生命周期最多成功进入一次动作调度；新会话不重置。
9. `async: true` 的动作进入后台任务集合，不阻塞 Agent；Prompt 注入和工具拦截禁止异步执行。
10. 工具执行前事件可以返回声明式拒绝；工具执行后事件只能观察结果，不能改写 `ToolResult`。
11. 任一 Hook 的匹配、模板或动作失败只产生本地脱敏诊断，不中断 Agent Loop，不递归触发 `system.error`。
12. 应用退出先发出 `system.shutdown`，再等待所有已登记后台任务完成各自的超时或取消收尾，最后关闭 MCP、artifact store 等资源。

## 3. 模块边界

新增模块：

```text
src/mewcode_agent/hooks/
├── __init__.py
├── models.py
├── loader.py
├── matching.py
├── templates.py
├── actions.py
├── engine.py
└── integration.py
```

| 模块 | 职责 |
| --- | --- |
| `hooks.models` | 事件名、规则、条件、动作、上下文、结果和诊断的不可变模型 |
| `hooks.loader` | 两层严格 YAML 解析、覆盖和组合约束校验 |
| `hooks.matching` | `exact/glob/regex/not` 值匹配和点路径精确取值 |
| `hooks.templates` | `${...}` 解析、上下文取值和安全字符串化 |
| `hooks.actions` | shell、Prompt、HTTP 和 subagent 占位执行器 |
| `hooks.engine` | 顺序分发、once、异步任务登记、超时、拦截和关闭归并 |
| `hooks.integration` | Agent Loop 与 ToolScheduler 的适配器 |

现有模块调整：

| 模块 | 调整 |
| --- | --- |
| `agent.loop` | 在会话、轮次、消息、压缩和错误边界分发 Hook 事件 |
| `agent.tool_scheduler` | 使用 Hook interceptor 分发工具执行前后事件并接受声明式拒绝 |
| `prompting.runtime` | 不改变既有模型；Hook Prompt 通过唯一 request instruction 注入 |
| `commands.builtins` | 新建或恢复会话时通知 Agent Loop 完成旧会话结束和新会话开始 |
| `cli.py` | 加载配置、构造引擎、打印诊断、发出启动/关闭事件并归并后台任务 |

## 4. 配置文件与覆盖

### 4.1 顶层结构

两个文件使用相同精确结构：

```yaml
version: 1
rules:
  - id: audit_write
    event: tool.before_execute
    once: false
    async: true
    timeout_seconds: 10
    match:
      tool.name:
        kind: exact
        pattern: write_file
    action:
      type: shell
      command: "python scripts/audit.py ${tool.name} ${file.path}"
      cwd: project
    intercept: null
```

顶层只允许且必须包含 `version` 与 `rules`：

- `version` 必须是整数 `1`，bool 无效；
- `rules` 必须是列表；
- YAML 重复键、未知键、非 UTF-8、根节点非 mapping 或语法错误使应用启动失败；
- 配置错误必须包含层级、精确规则索引或字段路径和稳定原因，但不包含动作正文、HTTP header 值或环境变量值。

### 4.2 规则字段

每条规则只允许且必须包含：

```text
id
event
once
async
timeout_seconds
match
action
intercept
```

| 字段 | 约束 |
| --- | --- |
| `id` | 完整匹配 `[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*` |
| `event` | 第 5 节列出的精确事件名之一 |
| `once` | 精确 bool |
| `async` | 精确 bool |
| `timeout_seconds` | 大于 `0` 且不超过 `300` 的有限整数或浮点数，bool 无效 |
| `match` | 字段名到 matcher 的 mapping；空 mapping 表示无条件匹配 |
| `action` | 第 8 节中的一个严格动作 mapping |
| `intercept` | `null` 或第 9 节的严格拦截 mapping |

同一层内 `id` 重复使该层配置无效。完成单层校验后，以用户规则为底、项目规则为覆盖层；项目中的同 ID 规则完整替换用户规则，并出现在项目规则的声明位置。最终执行顺序固定为：全部项目生效规则按声明顺序，然后是未被覆盖的用户规则按声明顺序。

## 5. 生命周期事件

事件名只接受以下精确值：

| 事件 | 触发边界 |
| --- | --- |
| `system.startup` | 全部核心组件构造成功、进入 UI 前 |
| `system.shutdown` | UI 退出后、关闭后台资源前 |
| `system.error` | Agent run 将稳定错误返回 UI 前 |
| `context.before_compaction` | 自动或手动整体摘要真正开始前 |
| `context.after_compaction` | 整体摘要完成或失败后 |
| `session.started` | 初始会话、`/clear` 新会话或 `/resume` 恢复会话激活后 |
| `session.ended` | 当前会话被切换或应用关闭前 |
| `round.started` | Prompt round 建立后、seal 与 Provider 请求前 |
| `round.ended` | round 完成、失败或取消后的 finally 边界 |
| `message.before_send` | 用户原始消息或计划反馈加入历史、进入下一次模型处理前 |
| `message.after_receive` | 一次 Provider turn 完整校验通过后 |
| `tool.before_execute` | 安全策略和审批通过后、调用 ToolRegistry 前 |
| `tool.after_execute` | ToolRegistry 或前置拦截返回稳定 `ToolResult` 后 |

不会为流式 text delta、thinking delta、单个 usage 事件或 Hook 自身诊断发出 Hook 事件。`message.after_receive` 的 `message.content` 是该 turn 的完整正文；工具调用轮正文为空时是空字符串，工具参数通过 `tool.*` 事件单独提供。

## 6. 事件上下文

### 6.1 通用字段

每个事件都有以下精确字段：

```text
event.name
event.sequence
project.root
session.id
```

- `event.sequence` 是当前 HookEngine 内从 `1` 开始递增的整数；
- `project.root` 是应用已解析的绝对工作目录字符串；
- `session.id` 是 SessionManager 的精确 active session ID；若构造期尚未建立会话则字段不存在；
- 不把整个进程环境、API Key、MCP header、Prompt system text 或用户配置正文放入上下文。

### 6.2 事件专属字段

| 事件族 | 精确字段 |
| --- | --- |
| `system.error` | `error.code`、`error.message` |
| `context.*` | `compaction.generation`、`compaction.covered_messages`、`compaction.estimate_before`、`compaction.estimate_after`、`compaction.success`、`compaction.error_code` |
| `session.*` | `session.restored` |
| `round.*` | `round.number`、`round.max_rounds`、`round.mode`、`round.outcome` |
| `message.*` | `message.content`、`message.kind` |
| `tool.*` | `tool.call_id`、`tool.name`、`tool.arguments_json`、`tool.arguments.<exact-key>` |
| `tool.after_execute` | 上述工具字段，加 `tool.result.success`、`tool.result.data`、`tool.result.error_code`、`tool.result.error_message` |

不存在的可选值对应字段直接缺失，不伪造空值。`tool.arguments.<exact-key>` 只展开成功解析为 JSON object 的直接键，不递归猜测或改写键名。

`file.path` 是唯一便利别名：只有工具参数 JSON object 含精确字符串键 `path` 且其值是字符串时才存在，值与 `tool.arguments.path` 完全相同。不会从 `file`、`filename`、`target` 或其他字段推断路径。

## 7. 条件匹配

### 7.1 `match` 结构

示例：

```yaml
match:
  tool.name:
    kind: regex
    pattern: "(?:write|edit)_file"
  file.path:
    kind: not
    pattern:
      kind: glob
      pattern: ".git/**"
```

`match` 的所有顶层字段隐式使用 AND；任一字段不存在或 matcher 返回 false，则规则不匹配。字段名必须完整匹配 `[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*`，按点分段精确读取，不 lower、不做别名、前缀或相似匹配。

### 7.2 Matcher

非 `not` matcher 只允许并必须包含 `kind` 与 `pattern`：

- `exact`：pattern 是 JSON scalar；值的 Python 类型和值都必须相同，bool 不等于整数；
- `glob`：pattern 必须是非空字符串；上下文值也必须是字符串，使用大小写敏感 `fnmatchcase` 对完整值匹配，不做路径分隔符归一化；
- `regex`：pattern 必须是非空有效 Python 正则；上下文值也必须是字符串，使用 `fullmatch`，配置加载时预编译验证。

`not` 只允许并必须包含：

```yaml
kind: not
pattern:
  kind: exact
  pattern: value
```

其 `pattern` 必须是另一个完整 matcher；最大递归深度固定为 `8`。`not` 只反转其子 matcher 的结果；顶层上下文字段仍必须存在，因此不能用 `not` 匹配缺失字段。

## 8. 动作

### 8.1 通用执行规则

1. 只有规则匹配后才解析模板和执行动作。
2. 每个字符串动作字段都支持 `${...}`，占位符路径遵循第 6 节精确字段。
3. 非字符串上下文值使用紧凑 UTF-8 JSON 序列化；字符串原文插入；缺失字段产生 `hook_template_field_missing`。
4. 模板替换不增加 shell quoting、URL encoding 或 JSON escaping。规则作者必须在动作字段中明确处理目标语法。
5. 每次动作都由 `asyncio.timeout(timeout_seconds)` 限制；超时产生 `hook_action_timeout`。
6. 动作结果不写入普通历史。只有 `prompt` 动作的内容进入 Prompt runtime control。

### 8.2 shell

精确结构：

```yaml
action:
  type: shell
  command: "python scripts/audit.py ${event.name}"
  cwd: project
```

只允许且必须包含 `type`、`command`、`cwd`：

- `type` 精确为 `shell`；
- `command` 是非空字符串；
- `cwd` 本章只接受精确字符串 `project`；
- Windows 使用当前 PowerShell 可执行环境，POSIX 使用 `/bin/sh`；
- cwd 固定为 `project.root`；继承当前进程环境，但不把环境值加入模板、诊断、模型或日志；
- stdout 和 stderr 都被消费以避免阻塞，但正文不进入模型或默认诊断；非零退出产生 `hook_shell_failed`。

### 8.3 prompt

精确结构：

```yaml
action:
  type: prompt
  content: "处理 ${file.path} 时遵循项目审计要求。"
```

只允许且必须包含 `type` 与 `content`。`content` 必须是非空字符串。Prompt 动作必须使用 `async: false`：

- 有活动 request 时，以唯一 `kind=instruction`、`scope=request` 控制消息注入，不加入 ConversationHistory 或 session JSONL；
- 无活动 request 时进入 FIFO pending 队列，在下一次 request 建立后、`message.before_send` 分发前注入；
- 新会话不丢弃尚未消费的 `system.startup` Prompt；`session.ended` 和 `system.shutdown` 的 Prompt 动作没有可消费边界，因此配置加载时禁止该组合；
- 注入失败只产生 Hook 诊断，不中断当前 Agent run。

### 8.4 http

精确结构：

```yaml
action:
  type: http
  method: POST
  url: "https://example.test/hooks"
  headers:
    Content-Type: application/json
  body: '{"event":"${event.name}"}'
```

只允许且必须包含 `type`、`method`、`url`、`headers`、`body`：

- `method` 只接受 `GET`、`POST`、`PUT`、`PATCH`、`DELETE`；
- `url` 和 header value 支持模板；header name 必须是非空单行字符串且不支持模板；
- `body` 是字符串，允许空字符串；
- 模板展开后的 URL 必须是绝对 `http` 或 `https` URL，必须有 host；
- 使用共享 `httpx.AsyncClient`，禁止自动重定向；任何非 `2xx` 状态产生 `hook_http_failed`；
- 响应正文和 header 不进入模型、历史或默认诊断。

### 8.5 subagent

精确结构：

```yaml
action:
  type: subagent
  task: "检查 ${file.path} 的测试影响"
  context: recent
```

只允许且必须包含 `type`、`task`、`context`：

- `task` 是非空字符串并支持模板；
- `context` 精确为 `summary`、`recent` 或 `none`；
- 本章占位执行器稳定产生 `hook_subagent_unavailable` 诊断，不启动 Agent、不写入历史、不视为应用启动错误；
- Chapter 11 接入时保持配置字段与错误隔离边界不变。

## 9. 工具拦截

`intercept` 非 null 时精确结构为：

```yaml
intercept:
  deny: true
  reason: "项目规则禁止执行 ${tool.name}"
```

约束：

1. 只允许且必须包含 `deny` 与 `reason`；`deny` 必须精确为 `true`，`reason` 必须是非空模板字符串。
2. 只有 `event: tool.before_execute` 可以声明非 null intercept。
3. 拦截规则必须使用 `async: false`。
4. 匹配后先在同一超时边界执行 action，再声明拒绝；即使 action 失败，声明式拒绝仍然生效。
5. 拒绝返回 `ToolResult(success=false, error_code="tool_blocked_by_hook")`，`error_message` 使用展开后的 `reason`；缺失模板字段时使用固定脱敏消息。
6. 命中第一个拦截后停止分发后续 `tool.before_execute` 规则；该拒绝结果仍分发一次 `tool.after_execute`。
7. Hook 拦截发生在 Chapter 04 安全策略和用户审批之后、实际 ToolRegistry 调用之前；它不能把安全策略拒绝改为允许，也不能绕过 plan-only 审批。

## 10. 同步、异步、once 与顺序

### 10.1 同步规则

- 按最终规则顺序逐条匹配和执行；
- 当前规则完成、失败或超时后才处理下一条；
- Prompt 注入在后续 Prompt compose 前可见；
- 工具拦截可以阻止实际执行。

### 10.2 异步规则

- 匹配和模板展开在事件分发调用内完成，动作本体通过 `asyncio.create_task` 启动；
- task 立刻登记到 HookEngine，不由调用方持有裸 task；
- 异步规则只允许 `shell`、`http` 或 `subagent`，禁止 `prompt` 和非 null `intercept`；
- 后台失败通过相同诊断处理器报告，不产生未获取异常；
- 同一事件内多个异步动作按规则顺序创建，但不保证完成顺序。

### 10.3 once

- `once: true` 的规则在匹配成功并准备进入同步动作或登记异步 task 时原子标记；
- 不匹配不消耗 once；动作失败或超时不重试；
- 标记只存在内存，不写入 session、notes 或配置；
- `/clear`、`/resume` 和 Prompt 压缩不重置；应用重启后重新开始。

## 11. Agent Loop 集成

### 11.1 会话

- CLI 构造完成后先发 `system.startup`，再发初始 `session.started(restored=false)`。
- `/clear`：先发旧 `session.ended`，成功建立新 session 并重置 Prompt/Skill 后发 `session.started(restored=false)`。
- `/resume`：先发旧 `session.ended`；恢复、修复、Prompt/Skill reset 完成后发 `session.started(restored=true)`。
- 若切换失败，旧会话保持活动，不发 `session.ended`；因此会话命令必须在 SessionManager 成功提交切换的原子边界调用生命周期适配器。
- 进程退出时先发当前 `session.ended`，再发 `system.shutdown`；每个事件最多一次。

### 11.2 请求与轮次

- request 成功建立后添加原始 user message，发 `message.before_send(kind=user)`；计划反馈同样逐字符发出。
- round 建立后发 `round.started`，再 seal 并 compose ProviderRequest。
- Provider turn 完整通过 usage、turn end 与 stop reason 校验后发 `message.after_receive(kind=assistant)`。
- round 的 finally 发 `round.ended`，`round.outcome` 精确为 `completed`、`continued`、`cancelled` 或 `failed`。
- 所有即将 yield `RunErrorEvent` 的路径先分发 `system.error`；Hook 失败不改变原 error code/message。

### 11.3 压缩

- 手动和自动整体摘要共用一个 Hook 适配边界；仅工具结果外置不触发整体压缩事件。
- `context.before_compaction` 在摘要 Provider 调用开始前发出。
- `context.after_compaction` 对成功、稳定失败和熔断跳过各发一次；`compaction.success` 精确表示是否生成了新 checkpoint。
- 压缩 Hook 不允许调用压缩命令本身；本章不增加斜杠命令或 Hook 递归入口。

## 12. Prompt 注入队列

Prompt action 由专用桥接器管理：

1. 为每次注入生成 `hook.prompt.event_<sequence>.rule_<id>` 形式的唯一 instruction ID；
2. ID 只使用已验证规则 ID 与整数 sequence，不使用模板正文；
3. 活动 request 内固定使用 request scope，使后续所有 round 可见；
4. request 外先保存渲染后的字符串和元数据，不保存完整事件上下文；
5. 下一 request 建立后按产生顺序一次性注入并清空 pending；注入失败的单项被诊断并丢弃，不阻塞后续项；
6. pending 不写磁盘、不进入 session archive、不被摘要或自动笔记读取；
7. 应用退出时丢弃无法再消费的 pending，只报告数量，不打印正文。

## 13. 错误与诊断

### 13.1 启动错误

配置文件结构、字段、matcher、正则、动作组合或覆盖结果无效时抛出 `HookConfigError` 并中止启动。消息必须定位到：

```text
<用户|项目> Hook 配置.rules[<index>].<field>
```

不允许跳过单条无效规则，因为部分加载会使自动化和拦截边界不可预测。

### 13.2 运行诊断

`HookDiagnostic` 固定包含：

```text
source
rule_id
event
action_type
code
message
```

稳定错误码至少包括：

| 错误码 | 含义 |
| --- | --- |
| `hook_context_invalid` | 内部事件上下文不满足模型约束 |
| `hook_match_failed` | 运行时匹配器失败 |
| `hook_template_field_missing` | 模板引用不存在字段 |
| `hook_action_timeout` | 动作超过规则超时 |
| `hook_shell_failed` | shell 启动失败或非零退出 |
| `hook_http_failed` | HTTP 请求失败或返回非 2xx |
| `hook_prompt_failed` | Prompt 注入失败 |
| `hook_subagent_unavailable` | Chapter 11 尚未接入子工作者 |
| `hook_background_cancelled` | 应用关闭时任务被取消 |

默认 stderr 诊断只显示上述字段，不显示 command、task、content、URL query、header/body、stdout、stderr、HTTP response、Prompt 正文、tool arguments 或 exception repr。

## 14. 关闭与资源管理

`HookEngine.close()` 固定顺序：

1. 拒绝新的普通事件分发；
2. 允许且只允许一次 `system.shutdown` 分发；
3. 关闭新后台任务登记入口；
4. 等待当前后台任务在各自 timeout 中结束；
5. 对调用方取消导致仍运行的 shell 子进程执行 terminate、等待，必要时 kill、再等待；
6. 关闭共享 HTTP client；
7. 清空未消费 Prompt 队列；
8. 返回关闭统计，不向外抛单个 Hook 动作错误。

重复 close 是幂等的。HookEngine 不自动清理配置、日志、会话、上下文 artifact 或任何用户文件。

## 15. 安全约束

1. YAML 只作为数据解析，不 import、不 eval、不执行构造标签。
2. 项目 Hook 可执行 shell 和发 HTTP，因此读取项目配置等同于用户明确信任该工作区；诊断和 README 必须说明这一点。
3. Hook 不拥有独立的安全策略豁免；它不能批准 ToolScheduler 请求、修改 permission mode 或伪造用户授权。
4. shell command 是规则作者明确声明的 shell 文本；引擎不把任意上下文字段自动拼入命令。
5. 模板不提供 `${env.*}`、`${config.*}`、`${prompt.*}` 或任意对象反射。
6. HTTP 不跟随重定向，避免规则目标静默迁移；响应内容不进入 Agent。
7. regex 在加载时编译并限制 pattern 长度为最多 `4096` 个字符；单次事件上下文字符串不由 Hook 复制进诊断。
8. Hook action 不能直接修改普通消息历史；Prompt action 只能通过 PromptRuntime control 注入。
9. 工具拦截只能 deny，不能 allow、替换参数、替换工具名或伪造成功结果。
10. Hook 自身错误不触发 `system.error`，防止递归风暴。

## 16. 非目标

1. 图形化规则编辑器、热重载、文件 watcher 或远程规则源。
2. 一个规则内声明多个 action、action 条件分支或 DAG。
3. Hook 修改工具参数、工具结果、普通历史、Provider 配置或权限决策。
4. HTTP OAuth、重试、重定向、代理配置或响应映射。
5. shell stdout/stderr 作为 Prompt 或工具结果。
6. 任意 Python callback、插件 import、eval 或动态代码加载。
7. 持久化 once 状态或后台任务恢复。
8. Chapter 11 前真实执行 subagent 动作。
9. Hook 配置自动创建、自动修复、自动删除或自动迁移。

## 17. 测试策略

### 17.1 Loader

- 两层缺失、空规则、项目覆盖、顺序与同层重复 ID；
- UTF-8、重复键、未知/缺失字段、version、类型、timeout 和事件名；
- 四类 action 精确字段与非法组合；
- intercept 仅限同步 tool.before_execute；prompt 禁止异步和无消费事件；
- 错误定位到精确 layer/rule/field，且不泄露动作正文。

### 17.2 Matcher 与模板

- exact 类型敏感；glob 大小写和完整值；regex fullmatch 与加载时错误；
- not 递归、深度上限、缺失字段不因 not 成功；
- 多字段 AND、空 match、点路径精确取值和 arguments 直接键；
- `${...}` 多次引用、非字符串 JSON、缺失字段和非法占位符；
- `file.path` 只接受精确 `path` 字符串键。

### 17.3 动作

- shell 平台 argv/cwd、stdout/stderr drain、非零、超时与取消收尾；
- prompt 活动 request 注入、pending FIFO、唯一 ID、历史与 archive 不变；
- HTTP method、URL、header/body 模板、无重定向、2xx、错误与超时；
- subagent 占位只产生稳定诊断。

### 17.4 引擎

- 项目优先顺序、同步串行、异步登记和完成乱序容忍；
- once 在匹配时消耗且失败不重试；
- 单规则失败不阻断后续规则或 Agent；
- 第一个拦截停止 before 规则、after 仍发一次；
- close 等待任务、获取异常、关闭 client、清理 pending 且幂等。

### 17.5 集成与回归

- startup/shutdown、session clear/resume、round、message、tool、compaction、error 的精确次数与上下文；
- ToolScheduler 安全拒绝不触发执行 Hook，审批通过后触发；
- Hook deny 返回 `tool_blocked_by_hook` 且 ToolRegistry handler 未调用；
- Prompt 注入出现在后续 compose，不进入 ConversationHistory；
- CLI 配置错误 fail-fast，运行诊断 stderr 脱敏；
- Chapter 01–09 全量默认测试继续通过；默认测试不读取真实用户 Hook、不执行真实 shell、不访问公网；
- 会话、笔记、artifact 和 Hook 配置均不自动清理。

## 18. 验收标准

1. 用户和项目两层配置按项目优先、同 ID 完整覆盖、稳定顺序加载。
2. 非法 YAML、字段、matcher、action 或组合在启动时给出精确脱敏定位。
3. 生命周期事件覆盖系统、压缩、会话、round、消息和工具边界。
4. 上下文精确提供事件、工具、文件、消息与错误字段，不猜测键名。
5. exact、glob、regex、not 和多字段 AND 按规格匹配。
6. `${...}` 可用于动作字段，缺失字段隔离为单次诊断。
7. shell、Prompt、HTTP 动作真实可用；subagent 使用稳定占位并由 Chapter 11 接管。
8. 同步动作按序阻塞，异步动作登记运行，once 在进程内至多调度一次。
9. 工具前置 Hook 可以 deny，不能越过权限策略或把 deny 改为 allow。
10. 任何 Hook 运行失败都不改变 Agent Loop 的原始成功、失败或取消结果。
11. 退出时发出 shutdown、等待后台任务收尾并关闭 HTTP/子进程资源。
12. Prompt Hook 不进入普通历史、session JSONL、摘要或自动笔记。
13. 配置与运行诊断不泄露 command、HTTP 数据、Prompt、tool arguments 或环境值。
14. Chapter 01–09 默认回归全部通过。
