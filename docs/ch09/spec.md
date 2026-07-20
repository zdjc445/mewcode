# Chapter 09 Specification：分层 Skill、按需激活与隔离执行

## 1. 范围与基线

- 前置实现：Chapter 03 Prompt 运行时、Chapter 04 工具安全、Chapter 06 上下文压缩、Chapter 07 会话边界、Chapter 08 集中式斜杠命令。
- 本章目标：把可复用 SOP、受限工具集和可选执行上下文组织成 Skill；启动只发现目录，模型或用户按需激活完整内容。
- 本章内置 `commit`、`review`、`test` 三个 Skill，既提供生产力能力，也作为共享与隔离模式的参考模板。
- 本章不实现 Skill 市场、远程安装、分发索引、依赖解析或版本管理。

## 2. 已确认决策

1. Skill 文档使用 UTF-8 Markdown，开头是严格 YAML frontmatter，正文是发给模型的 SOP。
2. Skill 来源优先级固定为项目级高于用户级高于内置级。
3. 项目根目录是 `<project_root>/.mewcode/skills/`，用户根目录是 `~/.mewcode-agent/skills/`，内置 Skill 位于 Python 包 `mewcode_agent/builtin_skills/`。
4. 同时支持单文件 `<name>.md` 和目录 `<name>/SKILL.md`；只有目录 Skill 可以携带 `tools.yaml` 与脚本。
5. 单个候选解析失败时跳过并记录脱敏诊断，不阻断其他 Skill；高优先级候选无效时允许低优先级同名有效 Skill 生效。
6. 完成覆盖后，任何生效 Skill 的 `allowed_tools` 引用了不存在的工具，应用启动立即失败。
7. 启动只向 Agent 注入生效 Skill 的 `name` 与 `description`，不加载正文到普通历史。
8. 内置系统工具名称固定为 `load_skill`，参数固定为 `name` 和 `arguments`。
9. 激活后的 SOP 固定钉在 Prompt 环境上下文中，每轮重新组装时都存在，不写入普通消息历史。
10. Skill 支持 `shared` 与 `isolated` 两种执行模式。
11. `load_skill` 是系统级工具，不受任何 Skill 的 `allowed_tools` 限制，因此激活后的 Skill 可以继续嵌套加载其他 Skill。
12. 目录工具通过 `tools.yaml` 声明，Python 脚本始终作为无 shell 的独立子进程执行。
13. 每个生效 Skill 自动注册 `/<name> [arguments]`；管理命令固定为 `/skills`、`/skills show <name>` 和 `/skills rescan`。
14. 每次执行 Skill 前重新读取其源文件；`/skills rescan` 重新扫描来源、重建目录和快捷命令。
15. `/clear` 清空普通历史时同时清空已激活 Skill，不保留上一会话的 SOP。

## 3. 模块边界

新增模块：

```text
src/mewcode_agent/skills/
├── __init__.py
├── models.py
├── loader.py
├── catalog.py
├── runtime.py
├── executor.py
├── tools.py
└── commands.py

src/mewcode_agent/builtin_skills/
├── commit/SKILL.md
├── review/SKILL.md
└── test/SKILL.md
```

| 模块 | 职责 |
| --- | --- |
| `skills.models` | Skill 元数据、来源、执行策略、诊断、工具声明和稳定错误 |
| `skills.loader` | frontmatter、`tools.yaml`、路径边界和严格字段解析 |
| `skills.catalog` | 三层扫描、同层唯一性、覆盖、重新扫描与目录快照 |
| `skills.runtime` | 激活集合、Prompt 控制消息、可见工具集和会话清理 |
| `skills.executor` | shared/isolated 调度、上下文选择和结果回流 |
| `skills.tools` | `load_skill` 与目录 Python 工具适配器 |
| `skills.commands` | `/skills` 管理命令和动态 `/<name>` handler |

现有模块调整：

| 模块 | 调整 |
| --- | --- |
| `tools.registry` | 提供完整工具快照、显式可见子集 schema 和 Skill 工具原子替换 |
| `agent.loop` | 每轮按 Skill 运行时决定可见工具；提供隔离运行入口 |
| `prompting.runtime` | 支持替换命名的 session context controls，不复制到普通历史 |
| `commands.registry` | 支持在安全重建事务中合并固定命令与动态 Skill 命令 |
| `commands.builtins` | `/clear` 通过显式服务清空激活集合 |
| `cli.py` | 构造 Skill catalog/runtime/executor，完成启动校验和命令注册 |

## 4. 存储与候选发现

### 4.1 来源目录

扫描根目录固定为：

```text
project: <project_root>/.mewcode/skills/
user:    ~/.mewcode-agent/skills/
builtin: mewcode_agent/builtin_skills/
```

用户目录无法解析时产生启动错误。项目和用户 Skill 根目录不存在表示该层为空，不自动创建目录。

内置资源通过 `importlib.resources` 读取，不依赖源码 checkout 的绝对路径。

### 4.2 支持形态

单文件 Skill：

```text
skills/
└── review.md
```

目录 Skill：

```text
skills/
└── example/
    ├── SKILL.md
    ├── tools.yaml
    └── tools/
        └── example_tool.py
```

规则：

1. 只扫描 Skill 根目录的直接子项，不递归寻找任意 Markdown。
2. 直接子文件只有扩展名精确为 `.md` 时是候选。
3. 直接子目录只有包含精确文件名 `SKILL.md` 时是候选。
4. 单文件 Skill 旁边的 `tools.yaml` 不与其关联。
5. 目录 Skill 的 `tools.yaml` 可选；不存在表示该 Skill 没有专属工具。
6. 不跟随解析后跳出对应 Skill 根目录的符号链接、junction 或相对路径。

### 4.3 名称来源

规范名称只取 frontmatter 的 `name`，不从文件名或目录名推断，也不自动修复。

`name` 必须完整匹配：

```regex
[a-z][a-z0-9-]*
```

文件名或目录名可以不同于 `name`，但诊断和详情必须同时显示规范名称与源路径。

## 5. Skill 文档格式

### 5.1 Frontmatter 边界

文档必须以逐字符匹配的 `---\n` 或 `---\r\n` 开始，并由后续单独一行 `---` 结束。结束标记之后是 Markdown SOP 正文。

缺少起始标记、结束标记、空 frontmatter、空正文、非 UTF-8、无效 YAML、YAML 重复键或 YAML 根节点不是 mapping 都使该候选无效。

frontmatter 只允许以下精确字段：

```yaml
name: commit
description: 提交当前工作区修改
allowed_tools:
  - read_file
  - run_command
execution_mode: shared
model: inherit
context_strategy: current
recent_messages: null
```

缺少字段或出现未知字段都使候选无效。

### 5.2 字段约束

| 字段 | 约束 |
| --- | --- |
| `name` | 符合 Skill 名称正则的非空字符串 |
| `description` | 非空单行字符串，不含 NUL |
| `allowed_tools` | 保持声明顺序的字符串列表；元素非空、精确、不得重复 |
| `execution_mode` | 精确为 `shared` 或 `isolated` |
| `model` | 本章只接受精确字符串 `inherit` |
| `context_strategy` | 精确为 `current`、`summary`、`recent` 或 `none` |
| `recent_messages` | `null` 或正整数，bool 不视为整数 |

组合约束：

- `shared` 必须使用 `context_strategy: current` 和 `recent_messages: null`；
- `isolated` 必须使用 `summary`、`recent` 或 `none`；
- `isolated + recent` 必须提供正整数 `recent_messages`；
- `isolated + summary|none` 必须使用 `recent_messages: null`；
- `model: inherit` 表示使用应用当前活动 Provider 和模型；本章不允许自由模型名或 Provider ID。

### 5.3 SOP 正文

- 正文去除首尾空白后必须非空；内部 Markdown 逐字符保留。
- loader 不解释正文中的链接、代码块、`@include`、模板变量或斜杠文本。
- 正文不具有配置权限，不会因为提到工具名而改变白名单。
- 热更新重新读取时使用磁盘上的完整新正文；已激活的旧正文以原子替换方式更新。

## 6. 目录工具清单

### 6.1 `tools.yaml` 顶层结构

精确结构为：

```yaml
version: 1
tools:
  - name: example_tool
    description: 一句话说明
    parameters:
      type: object
      properties: {}
      additionalProperties: false
    category: command
    timeout_seconds: 30
    script: tools/example_tool.py
```

顶层只允许 `version` 和 `tools`；两者都必需。`version` 必须是整数 `1`，`tools` 必须是列表。

每条工具只允许并必须包含：

```text
name
description
parameters
category
timeout_seconds
script
```

### 6.2 工具字段

- `name` 必须完整匹配 `[a-z][a-z0-9_]{0,63}`，并在全局工具注册中心唯一；不得以保留前缀 `mcp_` 开始，不得等于 `load_skill`；
- `description` 必须是非空单行字符串；
- `parameters` 必须是合法 JSON Schema object，并通过现有 `jsonschema` 校验器检查 schema 本身；
- `category` 本章必须精确为 `command`；
- `timeout_seconds` 必须是大于 `0` 且不超过 `300` 的整数或有限浮点数，bool 无效；
- `script` 必须是使用 `/` 分隔的相对 POSIX 路径，后缀精确为 `.py`；不得是绝对路径，不得包含空段、`.` 或 `..`；
- `script` 解析后的真实路径必须位于该目录 Skill 内且是普通文件；
- 同一清单内工具名重复使整个候选 Skill 无效。

### 6.3 注册与覆盖

目录工具属于声明它们的 Skill 候选。只有完成三层覆盖后选中的 Skill 才把专属工具加入全局工具目录；被覆盖候选的工具不注册，也不参与冲突检查。

生效专属工具与核心工具、MCP 工具、`load_skill` 或其他生效 Skill 工具同名时，Skill catalog 启动失败。不得根据来源优先级覆盖工具。

专属工具会进入该 Skill 可引用的名字空间，但不会自动加入 `allowed_tools`；只有明确列入白名单后才对激活该 Skill 的 Agent 可见。

## 7. 脚本执行协议

目录工具执行固定为：

```text
<sys.executable> <absolute-script-path>
```

约束：

1. 使用 asyncio 子进程 API，`shell=False`，不构造 shell 字符串。
2. 子进程工作目录固定为 Skill 目录的解析后绝对路径。
3. stdin 是一个 UTF-8 JSON object，内容是已经过 `parameters` 校验的工具参数；末尾附加单个换行。
4. stdout 必须是一个完整 UTF-8 JSON value，前后只允许 JSON whitespace；成功值作为 `ToolResult.data`。
5. stderr 不进入模型或普通历史；失败时只用于本地脱敏诊断，且不得在默认 UI 中显示正文。
6. 继承当前进程环境，不额外展开、替换或记录环境变量。
7. 超过 `timeout_seconds` 时终止子进程并返回稳定 `timeout`。
8. 非零退出、stdout 非 UTF-8、stdout 不是单个 JSON value、输出读取失败分别映射为稳定脱敏错误，不回显 stderr、traceback 或脚本源码。
9. 启动、扫描、帮助、详情和激活都不执行脚本；只有模型产生该工具调用并通过现有安全审批后才执行。
10. `category: command` 使脚本调用完整经过 Chapter 04 `ToolScheduler` 与 `SecurityPolicyEngine`，Skill 白名单不授予执行许可。

## 8. 三层扫描与覆盖

### 8.1 单层扫描

每层候选按相对路径的逐字符升序扫描，以保证日志和测试稳定。

单个候选的文档、清单或路径无效时：

- 整个候选被跳过；
- 产生一条 `SkillDiagnostic`，只包含来源、候选相对路径、稳定错误码和固定消息；
- 不包含 SOP、YAML 内容、脚本输出、exception repr 或用户目录之外的内部路径信息；
- 继续扫描其他候选。

同一层多个有效候选声明同一个 `name` 时，该名称在本层的所有候选都无效并分别记录冲突诊断；不得按文件名顺序任选一个。低优先级同名候选仍可生效。

### 8.2 覆盖

有效候选按 `builtin → user → project` 应用，同名后层原子替换前层。最终 catalog 每个名称只保留一个 `SkillDefinition`。

覆盖只作用于完整 Skill，不合并 frontmatter、正文、工具或文件。项目 Skill 覆盖用户 Skill 时，用户 Skill 的专属工具全部不可见且不注册。

### 8.3 启动 Fail-fast

候选解析和覆盖完成后执行以下全局验证；任一失败都会以 `SkillConfigError` 中止启动：

1. 生效 Skill 规范名称彼此唯一；
2. 生效专属工具名在全局唯一且不与现有工具冲突；
3. 每个 `allowed_tools` 名称都精确存在于最终工具目录；
4. 每个 Skill 名称不与固定内置命令的规范名称或别名冲突；
5. 所有动态 Skill 命令可以在同一个命令注册事务中成功注册。

解析失败诊断与 fail-fast 错误是两种不同边界：前者只淘汰单个候选，后者说明最终运行目录内部不一致，不能安全启动。

## 9. 两阶段加载

### 9.1 启动目录

启动 session control 只包含每个生效 Skill 的：

```text
name
description
```

目录按 `name` 逐字符升序渲染，并明确告诉 Agent：

- 需要使用某个 Skill 时调用 `load_skill`；
- 不得根据一句描述编造未加载的 SOP；
- `load_skill` 的 `name` 必须来自目录中的精确名称；
- 用户也可以通过 `/<name>` 直接执行。

目录不包含路径、source、allowed tools、execution mode、model、正文或工具 schema。

### 9.2 `load_skill`

Provider 工具 schema 固定为：

```json
{
  "type": "object",
  "properties": {
    "name": {"type": "string"},
    "arguments": {"type": "string"}
  },
  "required": ["name", "arguments"],
  "additionalProperties": false
}
```

行为：

1. 对 `name` 做精确查找，不 lower、不模糊匹配、不根据文件名猜测。
2. `arguments` 逐字符保留，由 Skill SOP 解释；loader 不进行 shell 或路径解析。
3. 执行前重新读取对应候选文档和 `tools.yaml`，完整解析并验证。
4. 热更新后的 `name` 必须仍等于当前 catalog 名称；否则本次加载失败并要求 `/skills rescan`。
5. 热更新后的工具集合、白名单、命令冲突或全局工具冲突必须重新验证；失败时保留旧 catalog 和激活状态。
6. `shared` Skill 激活后返回结构化状态，由当前主 Agent 在同一 request 中继续执行。
7. `isolated` Skill 立即创建隔离 Agent run；完成后把结果回流为本次 `load_skill` 的工具结果。

`load_skill` 始终存在于 Provider 可见工具集合，不受 active Skill 白名单交集、计划模式或 Skill 自带工具列表移除。它仍通过 scheduler 执行，但定义为系统只读控制工具，不执行文件写入或外部命令。

## 10. 激活状态与 Prompt 固定位置

### 10.1 激活集合

激活集合以 Skill `name` 为 key，保持首次激活顺序。重复激活同名 Skill：

- 重新读取并原子替换该 Skill 的完整定义；
- 更新对应 Prompt control 和工具白名单；
- 不在顺序中新增第二项；
- 不复制 SOP 到普通历史。

多个 shared Skill 可以同时激活，SOP 按首次激活顺序并存。

### 10.2 Session Controls

每个已激活 shared Skill 生成一个 `kind=context`、`scope=session` 的运行时控制项。内容明确分隔：

```text
Skill: <name>
Arguments:
<arguments>
SOP:
<Markdown body>
```

这些控制项在每轮 Prompt 重新构建时处于环境上下文区域，不加入 `ConversationHistory`、session JSONL、用户消息或 assistant 消息。

Skill 目录 control 与激活 control 都在 session reset 时重建。普通 request/round 结束不移除激活 Skill。

### 10.3 清理边界

以下操作清空激活集合：

- `/clear`；
- `/resume <session_id>`；
- 创建任何新 session；
- 应用进程退出。

激活集合不写入 session archive，因此恢复旧会话也不会恢复过去激活的 Skill。新 session control 仍包含当前重新扫描得到的 Skill 名称和描述目录。

## 11. 工具可见性与最小权限

### 11.1 可见集合

没有 active shared Skill 时，Provider 看见完整全局工具集合。

存在 active shared Skill 时，Provider 可见集合固定为：

```text
所有 active shared Skill 的 allowed_tools 交集
+ load_skill
```

交集按全局工具注册顺序输出，`load_skill` 保持其全局注册位置。采用交集是因为同时存在的 SOP 都必须满足自己的最小权限声明；不得使用并集扩大任一 Skill 的权限。

isolated Skill 的可见集合固定为其自身 `allowed_tools + load_skill`，不继承主会话 active Skill 的交集。

### 11.2 执行校验

ToolScheduler 在执行前再次检查调用名称是否属于该 run 的可见集合。即使历史中存在旧 tool call、Provider 返回隐藏工具名或注册中心仍拥有该工具，也返回稳定 `tool_not_visible`，不执行工具。

可见性只决定 Provider schema 和可调用集合，不替代：

- 路径沙箱；
- plan-only 写入拦截；
- `SecurityPolicyEngine` 规则；
- session/permanent 审批；
- 危险命令代码层拒绝。

## 12. Shared 执行模式

shared Skill 激活流程：

1. `load_skill` 热读取并验证定义；
2. 把 SOP 与本次 `arguments` 原子写入 active session controls；
3. 更新当前 run 后续轮次的可见工具集合；
4. 返回包含 `name`、`execution_mode` 和激活成功状态的结构化结果；
5. 当前 Agent 根据已固定的 SOP 继续同一主对话 request。

主会话现有历史完整保留，Skill 执行中的 assistant tool calls、tool results 和最终回答继续写入主会话历史与 JSONL。

shared Skill 的 `model` 必须是 `inherit`，使用当前 AgentLoop Provider，不切换模型。

## 13. Isolated 执行模式

### 13.1 隔离边界

isolated Skill 使用独立的：

- `ConversationHistory`；
- `PromptRuntime` request/round timeline；
- active Skill 集合；
- Agent run context；
- 工具可见性集合。

它共享当前 Provider、Prompt 静态模块、全局 ToolRegistry、ToolScheduler、安全策略、工作目录和上下文 artifact store。

隔离 run 的 user message 由固定模板组成，包含 Skill 名称、本次原始 `arguments` 和执行目标；完整 SOP 放在隔离 Prompt 的 session context control，不伪装成用户消息。

### 13.2 上下文策略

`summary`：先使用现有结构化上下文摘要器覆盖主会话当前完整普通历史，把最终 `ContextSummaryMessage` 与边界消息复制到隔离历史；不复制原始历史消息。摘要失败时本次 isolated Skill 失败，不降级为其他策略。

`recent`：从主会话 snapshot 尾部取最多 `recent_messages` 条完整消息。若切片首条是孤立 `tool` 消息，或 assistant tool calls 在切片中没有完整 tool results，则继续向前扩展到最近完整边界；仍不能组成完整历史时不携带该不完整工具事务。

`none`：隔离历史从空开始，不携带主会话普通消息、摘要或 checkpoint。

三种策略都不复制主会话 active shared Skill SOP；隔离 Skill 只固定自己的 SOP。隔离 run 内通过 `load_skill` 嵌套激活的 Skill 只存在于该隔离 run。

### 13.3 结果回流

隔离 Agent 的最终响应就是回流摘要，不再额外调用一次 LLM。它作为外层 `load_skill` 或 `/<name>` 调用的结构化结果返回，包含：

```text
name
execution_mode
result
```

隔离过程的内部 user/assistant/tool 历史、thinking、SOP 和上下文副本不写入主会话 JSONL。主会话只记录正常的外层工具调用和上述工具结果；用户直接使用 `/<name>` 时，命令层通过现有 Agent 入口发送固定合成请求，因此合成请求和最终主 Agent 响应正常持久化。

隔离 run 失败、取消、达到轮次上限或 Provider 错误时返回稳定脱敏 Skill 错误，不把部分内部历史回流。

## 14. 斜杠命令

### 14.1 管理命令

规范命令固定为 `skills`，无别名，类别为 `workflow`，类型为 `ui`：

```text
/skills
/skills show <name>
/skills rescan
```

`/skills` 按名称升序显示生效 Skill 的：

```text
name | description | source | execution_mode | active
```

`/skills show <name>` 精确查找并显示：

```text
name
description
source
source_path
execution_mode
model
context_strategy
recent_messages
allowed_tools
dedicated_tools
active
```

详情不显示 SOP 正文、工具脚本源码或环境变量。

`/skills rescan`：

1. 扫描三个来源到临时 catalog；
2. 解析候选并收集诊断；
3. 完成全局工具、白名单和命令冲突 fail-fast 校验；
4. 重建动态 Skill 命令和专属工具；
5. 对仍存在的 active shared Skill 重新读取并更新 SOP；
6. 已移除或变成无效的 active Skill 从激活集合删除；
7. 整个事务成功后一次性交换 catalog、命令目录、工具目录和 Prompt controls。

任何 fail-fast 错误都使 rescan 零修改。单候选诊断在成功 rescan 后通过系统消息显示数量和稳定摘要。

### 14.2 Skill 快捷命令

每个生效 Skill 注册一个规范名称为 Skill `name` 的公开 `agent` 命令：

```text
/<name> [arguments]
```

- 无别名；
- description 取 Skill `description`；
- category 固定为 `workflow`；
- argument hint 固定为 `传给 Skill 的可选原始参数`；
- handler 每次执行前重新读取源文件；
- 参数逐字符传给 `load_skill.arguments`；
- handler 通过 `CommandUI.send_user_message()` 发送固定合成请求，不直接调用 Provider、AgentLoop 或脚本。

固定合成请求为：

```text
请使用 Skill `<name>` 完成任务。必须先调用 `load_skill`，并严格遵循加载后的完整 SOP。
Skill 参数（原文）：
<arguments>
```

空参数时最后一行正文为空，仍显式传递 `arguments: ""`。原始斜杠命令不写入历史，合成 user message 正常写入。

### 14.3 命令冲突

Skill 名称不得与任何固定内置命令 name 或 alias 冲突，也不得与 `?` 冲突。冲突属于最终 catalog fail-fast 错误，不能通过项目优先级覆盖内置命令。

`/skills rescan` 需要动态重建 registry；对正在 dispatch 的旧 registry 不做原地修改，当前命令完成后 UI adapter 原子替换 controller 和补全目录。

## 15. 热更新一致性

热读取与 rescan 的区别：

- 执行前热读取只允许更新当前 catalog 已知候选的完整内容；`name` 必须保持不变，不发现新增或删除 Skill，不改变快捷命令集合；
- `/skills rescan` 重新发现新增、删除、重命名、覆盖和工具集合变化。

热读取使用临时对象完成全文、清单、路径、schema、工具冲突和白名单验证后才交换当前定义。失败时：

- 当前执行返回稳定错误；
- 已激活旧 SOP、旧工具与 catalog 保持不变；
- 不执行脚本；
- 不部分更新 Prompt controls。

## 16. 内置样板

### 16.1 `commit`

- `execution_mode: shared`
- `model: inherit`
- `context_strategy: current`
- 使用只读 Git 检查和明确的提交工作流；不得 push、创建 PR、重写历史或删除用户修改，除非用户在当前请求明确要求。
- `allowed_tools` 使用实际注册的核心读取、编辑和命令工具精确名称。

### 16.2 `review`

- `execution_mode: isolated`
- `context_strategy: recent`
- 使用有限最近消息获取用户指定范围，同时避免把完整主会话带入审查。
- SOP 固定只读，不修改文件；输出按严重程度列出可复现问题、精确文件与行号、测试缺口。

### 16.3 `test`

- `execution_mode: isolated`
- `context_strategy: summary`
- 读取项目与修改范围，选择并执行最小充分测试；报告精确命令、退出码、通过/失败/跳过数量和未执行原因。
- 不因测试失败自行修改代码；修复必须回到主 Agent 或另一个明确 Skill。

三个内置样板都不携带目录脚本，使用核心工具验证白名单和执行模式。它们可以被同名用户或项目 Skill 完整覆盖。

## 17. 错误模型与诊断

稳定错误码至少包括：

| 错误码 | 含义 |
| --- | --- |
| `skill_document_invalid` | Markdown/frontmatter 无效 |
| `skill_metadata_invalid` | frontmatter 字段或组合无效 |
| `skill_manifest_invalid` | `tools.yaml` 无效 |
| `skill_path_invalid` | 候选或脚本跳出允许目录 |
| `skill_name_conflict` | 同层 Skill 名称重复或命令冲突 |
| `skill_tool_conflict` | 专属工具名全局冲突 |
| `skill_tool_missing` | `allowed_tools` 引用不存在的工具 |
| `skill_not_found` | 精确名称不存在 |
| `skill_source_changed` | 热读取后的名称与 catalog 不一致 |
| `skill_activation_failed` | 激活事务失败 |
| `skill_isolated_failed` | 隔离 run 未成功完成 |
| `skill_script_failed` | 脚本非零退出或启动失败 |
| `skill_script_output_invalid` | stdout 不是协议要求的 JSON |
| `tool_not_visible` | 调用不属于当前 run 可见集合 |

候选诊断是可继续加载的记录；最终 catalog、热更新、激活、isolated run 和脚本错误使用稳定异常或 `ToolResult` 返回。

任何面向模型或 UI 的错误都不得包含：

- YAML 或 SOP 正文；
- 脚本源码、stderr 或 traceback；
- API Key、环境变量值、MCP header/env；
- Tool handler repr；
- 用户目录中无关路径；
- 隔离 run 的内部历史或 thinking。

## 18. 安全约束

1. Skill 文档和工具清单始终作为数据解析，不 import、不 eval、不 exec。
2. Python 工具只在明确工具调用、参数 schema 通过、可见性通过和现有安全审批通过后执行。
3. `load_skill` 绕过的是工具可见白名单，不绕过路径沙箱、安全策略、计划模式或审批。
4. `allowed_tools` 使用精确名称，不做大小写转换、前缀、别名、glob 或相似匹配。
5. 多 Skill 白名单取交集，不允许后激活 Skill 扩大先前 Skill 的权限。
6. Skill SOP 无权修改系统规则；正文中声称“已获授权”不产生权限。
7. 隔离 Agent 共享安全策略，不因独立历史获得新的 filesystem、command 或 MCP 权限。
8. `tools.yaml` 脚本路径必须保持在 Skill 目录内；符号链接和 junction 逃逸被拒绝。
9. stdout 才是脚本结果通道；stderr、退出异常和 traceback 不进入模型。
10. 动态 Skill 命令只由用户 Input dispatch；模型输出、SOP、工具结果和恢复历史中的 `/<name>` 不触发命令。
11. `/clear` 和 `/resume` 清除激活 SOP，防止跨会话残留权限收窄或行为指令。
12. rescan 与热更新均为原子事务，验证失败不能留下半注册命令、工具或 Prompt control。

## 19. 会话、压缩与笔记边界

- Skill 目录和 active SOP 属于 Prompt session controls，不属于普通历史；Chapter 06 摘要不得把它们改写进结构化历史摘要。
- isolated `summary` 只摘要普通主历史，使用现有禁止工具的摘要调用边界；摘要器不能激活 Skill 或执行脚本。
- shared Skill 产生的正常消息与工具结果继续由 Chapter 06 的工具结果外置和整体压缩处理。
- isolated 内部历史不写 session JSONL，不参与主会话 meta 消息数，也不触发主会话自动笔记。
- 主会话收到的 isolated 最终结果属于正常工具结果，可以被外置或后续摘要。
- `/clear`、`/resume` 和新 session activation 都重新生成 Skill 目录 control，并清空 active Skill controls。
- 会话仍不自动清理；Skill 运行不新增 session 自动删除路径。

## 20. 非目标

1. Skill 市场、在线索引、安装命令或发布流程。
2. Skill 版本字段、语义版本比较、锁文件、升级或依赖解析。
3. 远程 URL Skill、Git 仓库自动拉取或运行时下载脚本。
4. 非 Python 目录工具、长期驻留进程、进程池或脚本 RPC 多路复用。
5. shell 命令字符串作为工具实现。
6. 自定义工具 category、自动权限授予或跳过安全审批。
7. frontmatter 模板、环境变量插值、`@include` 或继承合并。
8. Skill 参数 schema；本章只传递原始字符串 `arguments`。
9. active Skill 跨会话持久化。
10. 同时为一个 shared 主会话选择多个不同模型。
11. 在本章配置自由模型名或任意 Provider ID。

## 21. 测试策略

### 21.1 文档与清单解析

- LF/CRLF frontmatter、缺失边界、空正文、重复键、未知/缺失字段、错误类型；
- name、description、allowed_tools、execution/model/context 组合和 recent_messages 边界；
- `tools.yaml` 顶层、工具字段、JSON Schema、timeout、script 路径与重复工具名；
- 单文件不关联清单，目录必须精确使用 `SKILL.md`；
- UTF-8、符号链接/junction 与路径逃逸。

### 21.2 扫描与覆盖

- 不存在目录为空；相对路径排序稳定；
- 单候选失败不阻断其他候选；
- 同层同名全部淘汰；
- project > user > builtin 完整覆盖；
- 高层无效时低层有效候选回退；
- 被覆盖 Skill 的工具不注册；
- 最终工具冲突、白名单缺失和命令冲突启动 fail-fast。

### 21.3 两阶段与 Prompt

- 启动 control 只含 name/description，不泄露正文或白名单；
- `load_skill` 精确 schema、精确名称、arguments 原文和未找到错误；
- shared 激活 SOP 进入 session context，不进入普通历史；
- 多 Skill 首次顺序、重复激活原子替换；
- `/clear`、`/resume`、新 session 清空 active 集合；
- session 恢复不恢复旧 active Skill。

### 21.4 工具可见性

- 无 active Skill 时完整目录；
- 单 Skill 白名单；多 Skill 取交集；
- `load_skill` 始终可见；
- hidden 工具调用在 scheduler 前后都不能执行；
- 可见工具仍经过 plan-only、安全规则、审批和路径沙箱；
- MCP 与目录专属工具使用相同精确名称规则。

### 21.5 Shared 与 Isolated

- shared 使用主历史并把结果正常持久化；
- isolated `summary`、`recent`、`none` 三种输入；
- recent 工具事务边界完整；
- isolated 独立 history/runtime/active skills；
- isolated 最终响应作为唯一结果回流，内部历史不持久化；
- 失败、取消、轮次上限和摘要失败不回流部分历史；
- 嵌套 `load_skill` 只影响对应主或隔离 run。

### 21.6 脚本协议

- 精确 Python argv、无 shell、cwd、UTF-8 JSON stdin；
- JSON scalar/object/array/null stdout；
- 参数 schema 拒绝不启动脚本；
- 非零退出、timeout、非 UTF-8、多余输出、无效 JSON 和大 stderr 脱敏；
- 扫描、help、show、activation 不执行脚本；
- category command 经过权限审批。

### 21.7 命令与热更新

- `/skills`、`show`、`rescan` 精确参数；
- 动态 `/<name>` 出现在 help 和补全，冲突 fail-fast；
- 合成 user message 逐字符稳定，arguments 原文保留；
- 执行前正文热更新；name 变化要求 rescan；
- rescan 新增/删除/覆盖与 active 更新；
- rescan 失败时 catalog、registry、tools 和 controls 零修改。

### 21.8 回归

- Chapter 01–08 全量默认测试继续通过；
- 默认测试不执行用户主目录真实 Skill，不启动真实用户脚本，不访问公网；
- 会话不自动清理；
- 用户原话、上下文压缩、自动笔记、MCP、命令和安全审批契约不变。

## 22. 验收标准

1. 项目、用户、内置三个来源按 project > user > builtin 完整覆盖。
2. 单文件与目录 Skill 都能严格解析；单候选错误被跳过并产生脱敏诊断。
3. 最终 catalog 的工具缺失、工具冲突和命令冲突在启动时 fail-fast。
4. 启动 Prompt 只公开 Skill 名称和一句说明。
5. `load_skill` 按需热读取完整 SOP，并始终不受 Skill 工具白名单移除。
6. active shared Skill SOP 固定在 session 环境上下文，不进入普通历史或 JSONL。
7. 多个 shared Skill 同时存在时工具白名单取交集，并始终加 `load_skill`。
8. shared Skill 使用当前主历史；isolated Skill 使用独立 history/runtime/active 集合。
9. isolated `summary`、`recent`、`none` 三种上下文策略均按精确边界工作。
10. isolated 最终响应作为唯一结果回流，内部历史和 thinking 不持久化。
11. 目录 Python 工具通过无 shell 子进程与 UTF-8 JSON stdin/stdout 协议执行。
12. 脚本只在可见性、schema 和现有安全审批全部通过后启动。
13. 每个 Skill 自动注册公开 `/<name>`，管理入口固定为 `/skills`、`show` 和 `rescan`。
14. 每次执行热读取源文件；完整发现变化只由 `/skills rescan` 原子应用。
15. `/clear`、`/resume` 与新会话清空 active Skill，恢复存档不恢复 SOP。
16. 内置 `commit`、`review`、`test` 样板覆盖 shared、isolated recent 与 isolated summary。
17. 本章没有市场、安装、远程下载或版本管理实现。
18. Chapter 01–08 默认回归全部通过。
