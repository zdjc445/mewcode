# Chapter 11 Specification：统一子工作者、Fork 与后台任务

## 1. 范围与基线

- 前置实现：Chapter 03 Prompt runtime、Chapter 04 工具安全、Chapter 06 压缩、Chapter 08 命令中心、Chapter 09 isolated Skill、Chapter 10 Hook。
- 本章目标：以一个稳定工具入口创建定义式或 Fork 式子工作者，使其独立运行 Agent Loop，并支持前台等待、无重启转后台、状态管理、结果通知和退出收尾。
- 本章中的“子工作者”是同一应用进程内的短期 Agent run；Chapter 13 的长期小组成员在其上增加持久身份、邮箱、共享任务和多后端。
- Chapter 12 才实现 Git worktree；本章先在角色元数据中保留精确 `isolation` 字段，并只执行 `none`。

## 2. 已确认决策

1. Provider 工具固定且只有一个：`spawn_worker`；角色增删不改变工具名称或参数 schema。
2. `spawn_worker.type` 指定预定义角色；省略 `type` 表示 Fork，不根据任务文本猜测角色。
3. 定义式使用空普通历史、固定角色 SOP 和可选独立 Provider profile；Fork 复制父历史完整边界、继承父 Prompt session controls 和父工具可见快照。
4. Fork 强制后台运行；第一条新 user message 注入不可嵌套、不可确认、直接执行和固定报告结构的强约束。
5. 所有子工作者从可见工具中移除 `spawn_worker`；角色白/黑名单继续收窄；后台模式再与全局后台白名单取交集。
6. 子工作者共享 Provider adapter、HookEngine、ToolRegistry 和文件系统基础设施，但拥有独立历史、Prompt request/round 状态、权限策略 session 规则、文件读取状态缓存、上下文压缩状态和 Token 统计。
7. 子工作者没有交互审批通道；遇到 `ask` 固定拒绝该次工具调用，并把拒绝结果回灌其 Agent Loop。
8. 子工作者跑到底：模型不再调用工具且返回非空正文时完成，最后正文是结果；不会停下来向用户追问。
9. 显式后台、前台超过阈值、ESC 手动移交三条路径都引用同一个运行中 task，不取消重建。
10. 后台终态形成结构化通知，延迟注入主会话的下一次 request，不中断当前 request，不写普通历史。
11. 任务记录在当前进程内一直保留，只有用户明确 cancel 才终止；不按数量或时间自动清理。
12. 内置角色固定为 `explore`、`plan`、`general` 和可配置启用的 `verify`。

## 3. 模块边界

新增模块：

```text
src/mewcode_agent/workers/
├── __init__.py
├── models.py
├── loader.py
├── catalog.py
├── usage.py
├── executor.py
├── manager.py
├── tools.py
└── commands.py

src/mewcode_agent/builtin_workers/
├── explore.md
├── plan.md
├── general.md
└── verify.md
```

| 模块 | 职责 |
| --- | --- |
| `workers.models` | 角色、运行配置、请求、任务状态、结果、通知、诊断和稳定错误 |
| `workers.loader` | 严格 frontmatter、运行配置、四层来源和最终引用校验 |
| `workers.catalog` | 来源覆盖、精确角色查找和工具描述目录 |
| `workers.usage` | 每个任务独立聚合 Provider usage |
| `workers.executor` | 定义/Fork 上下文、独立 runtime/policy/cache、Agent Loop 跑到底 |
| `workers.manager` | 并发、前后台移交、状态机、通知、取消和关闭 |
| `workers.tools` | 单一 `spawn_worker` Tool schema 与结果映射 |
| `workers.commands` | `/workers`、`/worker show`、`/worker cancel` |

现有模块调整：

| 模块 | 调整 |
| --- | --- |
| `prompting.runtime` | 导出当前 session controls 的精确快照，供 Fork 保持 Prompt 前缀 |
| `tools.file_state_cache` | 使用 ContextVar 提供每个 worker 独立的读取状态，并提供明确 clear |
| `tools.registry` | 暴露同一个 file-state runtime 边界，不复制动态工具对象 |
| `hooks.integration` | Prompt sink 使用 ContextVar 绑定当前 worker PromptRuntime |
| `hooks.actions` | Chapter 10 `subagent` 占位接入 WorkerManager 后台入口 |
| `agent.loop` | request 建立后同时 flush Hook pending 和 worker notifications |
| `app.py` | ESC 优先把活动前台 worker 原地移交后台，再取消父 Agent request |
| `cli.py` | 扫描角色、注册工具/命令、构造 manager，并按序关闭 workers→hooks→其他资源 |

## 4. 角色来源与覆盖

### 4.1 来源优先级

角色来源从高到低固定为：

```text
project: <project_root>/.mewcode/workers/
user:    ~/.mewcode-agent/workers/
builtin: mewcode_agent/builtin_workers/
plugin:  由插件适配器显式传入的只读角色根目录
```

- 每个根目录只扫描直接子文件，扩展名必须逐字符等于 `.md`；不递归扫描目录；
- 项目、用户和插件根目录不存在表示空来源，不自动创建；
- 内置根目录通过 `importlib.resources` 解析；
- 插件适配器 API 接受保持注册顺序的绝对 `Path` tuple；本章不扫描任意环境变量或 import 未声明模块；
- 有效候选按 `plugin → builtin → user → project` 完整覆盖，同名不合并字段、SOP 或工具列表；
- 同一来源内多个有效候选声明相同 `name` 时，该名称在本来源全部失效并记录诊断，低优先级有效定义仍可生效；
- 高优先级候选解析失败时只跳过该候选并记录脱敏诊断，允许低优先级同名定义回退。

插件根目录属于同一最低优先级；多个插件提供同名有效角色时，该名称在 plugin 层全部失效，不按注册顺序任选。

### 4.2 文件名与规范名称

规范名称只取 frontmatter `name`，不从文件名推断，也不转换大小写。名称必须完整匹配：

```regex
[a-z][a-z0-9-]{0,63}
```

文件名可以与名称不同；诊断同时记录 source 与相对 candidate，不显示 SOP 正文。

## 5. 角色 Markdown

### 5.1 精确 frontmatter

每个文件必须以 `---` 独占首行开始，并由后续 `---` 独占行结束。frontmatter 只允许且必须包含：

```yaml
name: explore
description: 只读探索代码与项目结构
allowed_tools:
  - read_file
  - find_files
  - search_code
denied_tools: []
model: inherit
max_rounds: 12
permission_mode: inherit
isolation: none
```

| 字段 | 约束 |
| --- | --- |
| `name` | 符合规范名称正则 |
| `description` | 非空单行 UTF-8 字符串，不含 NUL |
| `allowed_tools` | `null` 或保持顺序的精确工具名列表；`null` 表示以当前基础集合为准 |
| `denied_tools` | 保持顺序的精确工具名列表 |
| `model` | 精确 `inherit` 或 `llm_providers.yaml` 中存在的 Provider ID |
| `max_rounds` | `1..30` 的整数，bool 无效 |
| `permission_mode` | `inherit`、`strict`、`default` 或 `permissive` |
| `isolation` | `none` 或 `worktree`；本章运行时只执行 `none` |

工具名必须完整匹配 `[a-z][a-z0-9_]{0,63}`，列表内不得重复；同一名称不能同时出现在 allowed 与 denied。`spawn_worker` 不允许出现在 allowed 中，出现在 denied 中属于冗余但合法。

`allowed_tools` 或 `denied_tools` 在最终覆盖后引用不存在的工具时启动 fail-fast。校验时精确工具目录包含核心、MCP、Skill 专属工具和 `load_skill`，并把保留名称 `spawn_worker` 视为存在。

### 5.2 SOP 正文

- frontmatter 后的 Markdown 去除首尾空白后必须非空；内部正文逐字符保留；
- 正文作为 `kind=context`、`scope=session` 的 worker role control 注入，不写 user message、主历史或 worker 历史；
- loader 不解释正文中的命令、链接、模板、`@include` 或工具名；
- 角色文档只在应用启动时加载；本章不提供 watcher 或 rescan。

## 6. Worker 运行配置

用户级运行配置固定为：

```text
~/.mewcode-agent/workers.yaml
```

项目不允许改变并发、后台白名单或验证角色开关。文件不存在时使用代码内默认值；存在时必须是以下精确结构：

```yaml
version: 1
max_concurrency: 4
foreground_timeout_seconds: 15
background_allowed_tools:
  - read_file
  - find_files
  - search_code
  - read_context_artifact
enable_verify_role: false
```

约束：

- `version` 必须是整数 `1`；
- `max_concurrency` 是 `1..16` 的整数；
- `foreground_timeout_seconds` 是大于 `0` 且不超过 `300` 的有限数值；
- `background_allowed_tools` 是非空、无重复的精确工具名列表，不得包含 `spawn_worker`；
- 最终列表中的每个工具都必须存在，否则启动 fail-fast；
- `enable_verify_role` 是精确 bool；为 false 时，仅内置来源的 `verify` 候选不进入覆盖，用户或项目显式定义的 `verify` 仍然有效。

默认值就是示例中的值。运行配置不自动创建或写回。

## 7. 单一工具入口

### 7.1 工具定义

工具名固定为 `spawn_worker`，category 固定为 `read`。Provider schema：

```json
{
  "type": "object",
  "properties": {
    "task": {"type": "string"},
    "type": {"type": "string"},
    "background": {"type": "boolean"}
  },
  "required": ["task"],
  "additionalProperties": false
}
```

- `task` 去除首尾空白后必须非空，最大 `32768` 个 Unicode code point；内部原文保留；
- `type` 省略表示 Fork；存在时必须是非空规范角色名，并做精确 catalog 查找；
- `background` 省略默认 false；Fork 时无论传入何值都强制 true；
- 不接受 `role`、`agent`、`name` 等别名字段，不 lower、不模糊匹配。

工具 description 在启动时列出当前角色的精确 `name + description`，但工具名称与 schema 不随角色数量变化。

### 7.2 返回结构

前台完成：

```json
{
  "task_id": "<32 hex>",
  "status": "completed",
  "mode": "foreground",
  "type": "explore",
  "result": "<last assistant text>",
  "usage": {
    "prompt_tokens": 0,
    "cache_hit_tokens": 0,
    "cache_miss_tokens": 0,
    "completion_tokens": 0,
    "unavailable_rounds": 0
  }
}
```

进入后台：

```json
{
  "task_id": "<32 hex>",
  "status": "running",
  "mode": "background",
  "type": "fork",
  "transition": "explicit|fork_forced|timeout|escape"
}
```

前台 worker 失败返回稳定 `ToolExecutionError(worker_failed)`；并发已满为 `worker_capacity_reached`；角色不存在为 `worker_type_not_found`；Chapter 12 前请求 worktree 角色为 `worker_isolation_unavailable`。

## 8. 定义式执行

定义式 worker 使用：

- 空 `ConversationHistory`；
- 从父 PromptRuntime 精确复制的当前 session controls；
- 一个新增的 role context control，包含规范名称、任务边界和完整 SOP；
- 由 role `model` 选择的共享 Provider adapter；
- role `max_rounds`；
- 独立 ContextWindowManager、UsageCollector、SecurityPolicyEngine session rules 和 FileStateCache context；
- 当前工作目录和共享 ToolRegistry。

首条 user message固定为：

```text
执行下列子工作者任务。不要向用户提问，不要请求额外输入；在现有权限和工具范围内直接完成。没有更多工具需要调用时，输出最终结果并结束。

任务（原文）：
<task>
```

`model: inherit` 复用父 Provider 实例。其他值按精确 Provider ID 从已经加载的 `AppConfig.providers` 选择并在进程内缓存一个 adapter；不接受自由模型字符串或运行时 API key。

## 9. Fork 执行与 Prompt cache

### 9.1 历史边界

Fork 在 `spawn_worker` 工具实际执行时获取父历史。当前 assistant 工具批次尚未完整，因此：

1. 找到最后一个含 tool calls 的 assistant 消息；
2. 若其后 tool results 没有完整覆盖该消息全部 call ID，则删除该 assistant 消息及其后全部结果；
3. 对剩余前缀运行现有原子历史边界校验；
4. 逐字符复制完整前缀到 worker history。

不得复制半个工具事务、当前 `spawn_worker` call 或其他同批尚未完成的 tool result。

### 9.2 Prompt 与工具前缀

Fork 精确复制父 PromptRuntime 当前 session controls，包括项目/用户指令、笔记目录、Skill 目录和 active shared Skill controls；不复制活动 request/round controls。

Fork 的基础工具集合取调用时父 worker 的可见工具快照，保持全局注册顺序；随后强制移除 `spawn_worker` 并与后台白名单相交。除了这项防嵌套和后台收窄外，不重新发明工具集合。这样首个 ProviderRequest 的 system/session/history 前缀最大程度复用父请求，便于 Provider prompt cache 命中。

Fork 首条新增 user message 固定包含任务原文和以下强约束：

1. 不能调用 `spawn_worker` 或创建下一层 worker；
2. 不得主动对话、提问或请求确认；
3. 直接使用可见工具完成任务，遇到被拒绝工具时调整方案；
4. 没有工具调用时必须结束；
5. 最终报告不超过 `1200` 个 Unicode code point；
6. 最终报告按精确 Markdown 标题 `## Summary`、`## Evidence`、`## Risks`、`## Next Steps` 排列。

报告结构是强 Prompt 契约；代码保留最后正文原文并记录 `report_format_valid`，不会改写、摘要或伪造模型输出。

## 10. 工具过滤

有效工具集合按以下顺序计算：

1. 基础集合：定义式取当前 registry 全集；Fork 取父可见快照；
2. 全局禁止：移除 `spawn_worker`；
3. 角色允许：定义式 `allowed_tools != null` 时与其相交；
4. 角色拒绝：定义式减去 `denied_tools`；
5. 后台收窄：后台任务与 `background_allowed_tools` 相交；
6. 按 registry 原始注册顺序生成最终 `frozenset`。

过滤只控制 Provider schema 和 ToolScheduler 可见性，不授予权限。最终工具仍经过路径边界、角色独立 SecurityPolicyEngine、Hook before/after 和底层 ToolRegistry 参数校验。

后台白名单即使列出 write/command 工具也只表示可见，仍受 permission mode；默认白名单只含读取工具。

## 11. 权限与运行状态隔离

### 11.1 权限

- 每个 worker 从启动时 `SecurityConfiguration` 构造新 SecurityPolicyEngine；不继承父 session 临时允许；
- user/project/permanent 规则和代码层 boundary 仍共享；
- `permission_mode: inherit` 使用启动配置 mode，其他值只覆盖该 worker engine；
- worker 收到 ToolApprovalRequestedEvent 时固定 resolve 为 `reject`，不打开父 UI，也不保存 session/permanent 允许；
- Fork 的“不要请求确认”是 Prompt 约束；代码层固定 reject 是最终防线。

### 11.2 文件状态

共享核心 Tool 实例使用 ContextVar 选择 FileStateCache state：

- 主 Agent 使用默认 state；
- 每个 worker task 绑定全新 state mapping；
- worker 内并发工具 task 继承同一 mapping；
- 不同 worker 和主 Agent 不共享“已读取”记录；
- worker 完成或取消后 state 随 context 释放，不写磁盘。

### 11.3 Token

每个 worker 独立统计所有 Agent round 的 available usage：`prompt_tokens`、`cache_hit_tokens`、`cache_miss_tokens` 和 `completion_tokens`。usage unavailable/invalid 时不猜测 token，分别累计 `unavailable_rounds`。Worker 统计不写入主 UsageCollector。

## 12. Hook 继承

worker AgentLoop 使用同一个 HookEngine，因此 round、message、tool、error 和 compaction Hook 继续生效。Prompt Hook 通过 ContextVar 绑定到当前 worker PromptRuntime：

- worker task 内触发的 Prompt action 只注入该 worker；
- 主 Agent Hook 仍注入主 PromptRuntime；
- 异步 Hook task 继承创建时的 Prompt target；
- worker 完成后未消费的 worker pending Prompt 被丢弃，不进入主会话；
- shell/HTTP/once 仍共享同一个 HookEngine 进程状态。

Chapter 10 的 `subagent` action 在本章接入 WorkerManager：始终创建后台 worker，不阻塞 Hook 分发。`context` 映射为：

- `none`：`general` 定义式空历史；
- `recent`：Fork 基础，但只复制最近 `12` 条并向前扩展到完整工具边界；
- `summary`：使用禁止工具的现有 ContextSummarizer生成父历史结构化摘要，再启动 `general` worker。

Hook subagent 同样经过并发上限、后台白名单、自身工具禁止和独立权限；启动失败只回到 Hook 诊断，不影响主 Agent。

## 13. 前台与后台状态机

### 13.1 状态

任务状态固定为：

```text
starting → running → completed
                   ↘ failed
                   ↘ cancelled
```

运行模式固定为 `foreground` 或 `background`。模式只允许单向 `foreground → background`，终态后不可改变。

### 13.2 三条后台路径

1. 显式：`background: true`，启动成功后立即返回，transition=`explicit`；无 `type` 的 Fork 强制走此机制并改记 transition=`fork_forced`；
2. 超时：定义式前台等待超过 `foreground_timeout_seconds`，只切换记录和等待方，transition=`timeout`；
3. ESC：ChatApp 检测活动 foreground task，先调用 manager detach，再 cancel 父 AgentRunContext，transition=`escape`。

三种机制都不 cancel worker `asyncio.Task`、不新建 history/runtime、不重复首轮 Provider 请求。

竞态规则：worker terminal 与 detach 同时发生时，terminal 优先；已经 terminal 的任务不能被标记 background。全局最多一个 foreground 等待方，但后台和独立 worker 总数受 `max_concurrency`。

### 13.3 外层取消

若 Tool 调用协程因外层取消或 Registry timeout 被取消，WorkerTool 先把仍运行的同一 worker 标记 background，再重新抛出取消；worker task 使用独立 task ownership，不随等待方取消。

## 14. Manager 记录与通知

### 14.1 任务记录

每条记录固定包含：

```text
task_id
session_id
type
kind (definition|fork|hook)
state
mode
transition
task
provider_id
model
visible_tools
created_at
started_at
ended_at
usage
result
error_code
report_format_valid
```

`task_id` 是 `uuid4().hex` 的 32 位小写十六进制字符串。时间使用带时区 ISO 8601。内部更新在 asyncio lock 下原子完成；对 UI/命令只返回不可变 snapshot。

错误结果不保存 exception repr、traceback、Provider raw body、Prompt、API key 或 Hook action 数据。

### 14.2 通知

只有曾进入 background 的任务在 terminal 时排队通知。通知是精确紧凑 JSON control，包含：

```json
{
  "type": "worker_terminal",
  "task_id": "...",
  "worker_type": "...",
  "status": "completed|failed|cancelled",
  "usage": {},
  "result": "最多 8000 code points",
  "error_code": null
}
```

- 完整结果保留在任务记录；通知 result 超过 `8000` code point 时只保留头尾和明确截断标记；
- 通知在主 Agent 下一 request 建立后作为唯一 `kind=context`、`scope=request` control 注入；
- 不加入 ConversationHistory、session JSONL、摘要或自动笔记；
- 当前 request 运行中完成的任务不会注入该 request 的后续 round；
- 会话切换清除上一会话未消费通知，但任务记录和结果仍可通过命令查看；
- 同一任务最多排队和消费一次通知。

## 15. 内置角色

### 15.1 `explore`

- 只读理解代码、文件结构和现状；
- allowed：`read_file`、`find_files`、`search_code`、`read_context_artifact`；
- denied：空；model/permission inherit；max rounds `12`；isolation none。

### 15.2 `plan`

- 基于证据制定可执行计划，不修改文件；
- 与 explore 相同读取集合；max rounds `10`。

### 15.3 `general`

- 通用执行；`allowed_tools: null`，由安全规则和运行模式决定；
- denied 只显式列 `spawn_worker`；max rounds `15`。

### 15.4 `verify`

- 读取变更并运行验证，不自行修复；
- allowed 增加 `run_command`；permission inherit；max rounds `12`；
- 内置候选只有 `enable_verify_role: true` 时生效；用户或项目同名定义不受此开关隐藏。

内置角色引用 `read_context_artifact` 时，CLI 必须在 artifact tool 注册后扫描。测试中的精简 registry 可显式排除该内置候选或提供同名替身，不根据名称猜测工具。

## 16. 命令

公开命令固定为：

| 命令 | 行为 |
| --- | --- |
| `/workers` | 列出全部任务，显示 task ID、type、state、mode、usage 和起止时间 |
| `/workers roles` | 列出生效角色 name、description、source、model、max rounds 和 isolation |
| `/worker show <task_id>` | 显示单任务完整脱敏 snapshot，包括完整 result |
| `/worker cancel <task_id>` | 经确认后取消非 terminal 任务并等待收尾 |

`task_id` 只接受 32 位小写十六进制；子命令区分大小写，不做模糊匹配。命令不进入普通历史。取消 terminal 或未知任务返回稳定本地错误，不影响 Agent。

## 17. 关闭

`WorkerManager.close()` 固定：

1. 停止接受新任务；
2. 对所有 running task 设置 AgentRunContext cancel；
3. cancel 仍未结束的 asyncio task；
4. gather 所有 task 并获取异常；
5. 把记录更新为 completed/failed/cancelled 的确定终态；
6. 清空未消费通知和 worker Prompt pending；
7. 返回关闭统计；重复 close 幂等。

CLI 在 UI 退出后先关闭 WorkerManager，确保最后的 worker Hook 可以完成；随后刷新笔记、发 session/system shutdown Hook，再关闭 session、MCP 和 artifact store。WorkerManager 不删除用户文件、会话、role config 或 Git 修改。

## 18. 错误与诊断

启动/扫描错误：

| 错误码 | 含义 |
| --- | --- |
| `worker_document_invalid` | Markdown/frontmatter 无效 |
| `worker_metadata_invalid` | 角色字段、组合或 SOP 无效 |
| `worker_name_conflict` | 同层角色重名 |
| `worker_tool_missing` | 最终角色或后台白名单引用不存在工具 |
| `worker_model_missing` | 角色 model 引用不存在 Provider ID |
| `worker_config_invalid` | `workers.yaml` 无效 |

运行错误：

| 错误码 | 含义 |
| --- | --- |
| `worker_type_not_found` | `type` 精确名称不存在 |
| `worker_capacity_reached` | 达到并发上限 |
| `worker_isolation_unavailable` | Chapter 12 前请求 worktree |
| `worker_history_invalid` | Fork 父历史不能组成完整前缀 |
| `worker_failed` | Agent run 或 Provider 稳定失败 |
| `worker_cancelled` | 任务被显式取消或应用关闭 |
| `worker_task_not_found` | 命令 task ID 不存在 |
| `worker_task_terminal` | 不能取消 terminal 任务 |

诊断和错误不得包含 API key、Provider raw response、thinking、完整 Prompt、Hook action、审批 fingerprint、exception repr 或 traceback。

## 19. 安全约束

1. 角色 Markdown 只作为数据解析，不 import、不 eval、不执行正文。
2. `spawn_worker` 无论全局目录、角色 allowed、父工具集或 Hook task 都不能出现在 worker 可见集合。
3. 后台工具集合只会收窄，不会因角色 allowed 扩大父集合或后台白名单。
4. Fork 复制历史与 Prompt context 不等于继承权限；worker SecurityPolicyEngine session 规则为空。
5. worker 没有 UI 审批通道，ask 固定拒绝；模型文字声称已授权不产生权限。
6. 共享文件系统意味着 worker 的已允许写操作真实影响项目；默认后台白名单只读，write/command 必须由用户显式配置并仍经过策略。
7. 文件状态 cache 隔离防止 worker 借用父 Agent 的“已读取”记录通过 edit/write 乐观锁。
8. 任务结果和通知不包含 thinking；Provider/工具稳定错误保持现有脱敏边界。
9. ESC 只改变 worker ownership，不绕过父 Agent 取消，也不复制执行。
10. 任务记录不自动清理；取消不回滚已经完成的文件或命令副作用。

## 20. 非目标

1. 长期成员身份、邮箱、共享任务 DAG、Lead 调度和跨进程 pane 后端（Chapter 13）。
2. Git worktree 创建、切换、保留和清理（Chapter 12）。
3. worker 之间递归 spawn、任意深度树或动态提升并发。
4. 把子工作者流式 token 直接渲染到主 transcript。
5. 子工作者交互式问答或转发审批 modal。
6. 任务记录跨进程持久化或重启恢复。
7. 角色 hot reload、rescan、市场、远程下载或版本管理。
8. 自由 base URL、API key、模型名或未配置 Provider 的运行时选择。
9. 自动清理任务记录、会话、角色文件或 worker 产生的项目修改。

## 21. 测试策略

### 21.1 Loader 与 catalog

- frontmatter 边界、重复键、未知/缺失字段、类型、正文和 UTF-8；
- name、tool lists、model、rounds、permission、isolation；
- project > user > builtin > plugin 覆盖、高层无效回退、同层冲突；
- verify 开关、工具缺失、model 缺失和 spawn_worker allowed 拒绝；
- runtime config 默认值、严格字段、并发、timeout 和后台白名单。

### 21.2 执行隔离

- 定义式空历史、role SOP、独立 Provider、max rounds；
- Fork 完整历史前缀、当前不完整工具批次剔除、session controls 和父可见工具快照；
- 首个 Fork ProviderRequest 保持父 system/history/tools 前缀；
- 独立 policy session rules、固定 reject approval、独立 file state cache 和 usage；
- Hook 在 worker 生效，Prompt Hook 进入 worker runtime 而非主 runtime；
- 最后一条非工具文本回流，失败/取消不回流部分 thinking/history。

### 21.3 过滤与状态机

- self tool 全局移除、role allow/deny、background 交集及顺序；
- 显式后台、Fork 强制、timeout 和 ESC 原地移交使用同一 task ID/实例；
- terminal/detach 竞态、并发上限、取消和幂等 close；
- Tool 等待方取消后 worker 继续并产生通知。

### 21.4 通知与命令

- background terminal 通知一次、下个 request 注入、不进历史、8000 字截断；
- 当前 request 不被完成通知打断；session switch 清 pending 但保留任务记录；
- `/workers`、roles、show、cancel 的精确参数、确认和 terminal 错误；
- App ESC 有 foreground 时 detach 后 cancel parent，无 foreground 时保持原 cancel 行为。

### 21.5 回归

- Chapter 01–10 全量默认测试继续通过；
- 默认测试不调用真实 Provider、不读取真实用户 worker 文件、不启动公网请求；
- 会话、笔记、Hook、worker 记录和项目文件没有新增自动删除路径。

## 22. 验收标准

1. 角色按 project > user > builtin > plugin 完整覆盖，单候选错误隔离，最终引用 fail-fast。
2. `spawn_worker` 是唯一稳定工具入口，精确 `type` 选择定义式，省略进入 Fork。
3. 定义式支持空历史、固定 SOP、独立 Provider profile、max rounds 和 permission mode。
4. Fork 继承完整父历史/session controls/可见工具快照，并剔除当前不完整工具批次。
5. Fork 强制后台并注入禁止嵌套、禁止确认、直接执行和结构化限长报告指令。
6. self tool、角色限制和后台白名单形成只收窄不扩大的多层防线。
7. 历史、Prompt runtime、policy session rules、file read cache、compaction 和 usage 在 worker 间隔离。
8. Provider、Hook、ToolRegistry 和文件系统基础设施共享；Prompt Hook 精确进入触发它的 worker。
9. 子工作者跑到底，最后非工具正文作为结果，ask 固定拒绝且不等待 UI。
10. 显式、Fork、timeout、ESC 都能进入后台；前台移交不取消或重启运行中实例。
11. Manager 维护状态、结果、usage、起止时间和完整记录，并遵守并发上限。
12. 后台 terminal 通知只在主会话下一 request 注入一次，不中断当前对话、不写普通历史。
13. explore、plan、general 和按开关启用的 verify 内置角色可用。
14. `/workers`、`/workers roles`、`/worker show` 和确认 cancel 可管理任务。
15. Hook subagent 动作在本章接入真实后台 worker，错误仍不影响主 Agent。
16. 退出会取消并 gather worker，不留下未获取 task 异常，不自动删除用户数据。
17. Chapter 01–10 默认回归全部通过。
