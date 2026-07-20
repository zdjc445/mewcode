# Chapter 07 Specification：项目指令、会话存档与分层自动笔记

## 1. 文档状态

- 状态：规范草案，尚未实现。
- 前置实现：Chapter 03 Prompt 时间线、Chapter 04 工具安全边界、Chapter 06 上下文压缩。
- 本章目标：加载可审计的用户级与项目级 Markdown 指令，持续保存并安全恢复会话，并用无工具 LLM 请求维护可人工编辑的用户级和项目级笔记。

## 2. 已确认需求与固定决策

1. 用户级指令文件精确为 `Path.home() / ".mewcode-agent" / "INSTRUCTIONS.md"`。
2. 项目级指令文件精确为 `<project_root> / "MEWCODE.md"`。
3. 项目级指令优先于用户级指令，并在 Provider 时间线中排在用户级指令之前。
4. 指令文件支持独占一行的 `@include`，只允许引用所属配置根目录内的相对路径。
5. include 最大深度为 `5`；单文件上限 `64 KiB`，单层合并上限 `256 KiB`。
6. 会话默认自动保存到 JSONL 和独立 `meta.json`。
7. 会话不做任何基于数量、年龄或磁盘占用的自动清理。
8. 会话删除只能通过精确命令和确认界面进行。
9. 恢复时跳过不可解析记录；工具调用不完整时截断到最后完整原子边界。
10. 恢复历史超过 Prompt 预算时先尝试一次现有上下文压缩。
11. 距离上次活跃时间至少 `7` 天时注入代码生成的时间跨度提醒。
12. 自动笔记每 `5` 个成功完成的用户请求触发一次；应用退出时对尚未处理的新对话再触发一次。
13. 笔记更新使用当前 Provider，`tools=None`，由 LLM 负责语义去重，不实现相似度算法。
14. 用户偏好和纠正反馈写入用户级笔记；项目知识和参考资料写入项目级笔记。
15. 用户级笔记精确为 `Path.home() / ".mewcode-agent" / "notes.md"`。
16. 项目级笔记精确为 `<project_root> / ".mewcode" / "notes.md"`。
17. “编辑”命令只显示精确文件路径，不启动或猜测外部编辑器。

## 3. 当前实现事实与接入约束

### 3.1 Prompt 时间线

`PromptRuntime` 当前拥有 session、request 和 round 三种作用域。指令文档和笔记必须作为 session control 注入，并位于第一条真实用户消息之前：

- 指令文档使用 `kind="instruction"`；
- 自动笔记使用 `kind="context"`，不能成为新的授权来源；
- 普通项目文件、include 内容和 LLM 笔记不能覆盖 `core.authorization`、`core.safety` 或代码层安全策略。

恢复会话时不恢复旧 `ControlMessage`。新进程重新生成当前环境、当前指令、当前笔记和后续 request/round control。

### 3.2 普通历史与工具结果

`ConversationHistory` 保存不可变 `ChatMessage`。Chapter 06 可能把内存中的大型 tool message 替换为当前进程 artifact 预览，但 artifact 在正常退出时删除。

因此会话 JSONL 必须记录首次加入历史的原始 tool result，不记录后续内存预览替换。恢复时重新得到完整 tool result，再由当前进程的 Layer 1 外置到新的 artifact session。不得把已经失效的旧 artifact 路径保存为跨进程事实。

### 3.3 会话切换

`/resume` 在同一个 Textual 进程中切换会话。切换必须同时重置：

1. 普通 `ConversationHistory`；
2. `PromptRuntime` 的 request counter、round 状态和非持久时间线；
3. `ContextWindowManager` checkpoint、估值基线、自动失败计数和单请求尝试状态；
4. `ToolResultCompactor` 已处理批次索引。

任何上一会话的摘要、request control、round control 或估值基线都不能进入恢复会话。

## 4. 项目范围

### 4.1 范围内

1. 两层 Markdown 指令文件和受限 `@include`。
2. 指令优先级、独立 Prompt 消息和运行时安全语义。
3. 自动创建、追加保存、列出、定位、恢复和手动删除会话。
4. JSONL 记录校验、坏行跳过、工具批次截断和异常恢复重写。
5. `meta.json` 的 O(1) 会话列表读取。
6. 恢复后的 Token 压力处理和时间跨度提醒。
7. 两层 Markdown 笔记的加载、注入、自动更新、查看、定位和手动清空。
8. 笔记 LLM 请求的工具禁用、固定结构、大小限制、合并和原子写入。
9. 会话与笔记命令的精确解析、并发限制和确认界面。

### 4.2 范围外

1. 云同步、团队共享、数据库、向量数据库或语义检索。
2. 会话自动删除、容量淘汰、按年龄清理或后台归档。
3. 跨项目自动合并项目指令或项目笔记。
4. 从父目录递归发现多份项目指令。
5. `@include` URL、glob、环境变量、模板表达式或条件语法。
6. 自动执行笔记中的命令、路径或工具调用。
7. 自动启动 VS Code、记事本、`$EDITOR` 或其他外部编辑器。
8. 手工修改 JSONL 后的智能冲突合并。
9. 把笔记或会话摘要当作文件内容、代码状态或用户授权的权威来源。

## 5. 新增模块

新增包和文件精确为：

```text
src/mewcode_agent/instructions/
  __init__.py
  loader.py
  models.py

src/mewcode_agent/sessions/
  __init__.py
  manager.py
  models.py
  storage.py

src/mewcode_agent/notes/
  __init__.py
  manager.py
  models.py
  storage.py
  updater.py
```

职责如下：

| 模块 | 职责 |
| --- | --- |
| `instructions.models` | 指令层、加载结果与稳定错误 |
| `instructions.loader` | 精确路径读取、include 展开、循环和边界校验 |
| `sessions.models` | JSONL record、meta、恢复结果与稳定错误 |
| `sessions.storage` | 追加写、fsync、meta 原子替换、恢复扫描和修复重写 |
| `sessions.manager` | 当前会话、列表、恢复、路径和删除事务 |
| `notes.models` | 四类笔记、更新结果与稳定错误 |
| `notes.storage` | Markdown 严格解析、渲染、原子写入和清空 |
| `notes.updater` | 无工具 LLM 请求、严格 JSON 解析与 usage |
| `notes.manager` | 五请求触发、任务合并、退出 flush 和 Prompt 注入回调 |

## 6. Markdown 指令文件

### 6.1 根路径与缺失规则

| 层 | 根目录 | 入口文件 |
| --- | --- | --- |
| 用户级 | `Path.home() / ".mewcode-agent"` | `INSTRUCTIONS.md` |
| 项目级 | 应用启动时确定的 `project_root` | `MEWCODE.md` |

入口文件不存在表示该层没有指令，不报错。入口存在但不是普通文件、无法读取、不是有效 UTF-8 或超过上限时，应用拒绝启动并返回稳定脱敏错误。

项目根目录精确等于现有 `SessionEnvironment.working_directory`，不向父目录搜索其他 `MEWCODE.md`。

### 6.2 `@include` 语法

directive 必须去除行首尾空格后完整匹配：

```text
@include <relative-path>
```

规则：

1. `<`、`>` 必须存在且每行只能出现一个 path。
2. path 是 `<` 与 `>` 之间的原始字符串，只去除两端 ASCII space 和 tab。
3. path 不能为空，不能包含 NUL，不能是绝对路径。
4. 不展开 `~`、环境变量、glob、URL、反斜线转义或 Markdown link。
5. directive 必须独占一行；正文中的 `@include` 不触发展开。
6. 被 include 文件可以使用任意扩展名，但仍按严格 UTF-8 文本读取。
7. include 正文在 directive 原位置替换，文件末尾统一保留一个换行。

### 6.3 边界、循环与深度

每一层以自己的根目录作为唯一 sandbox：

1. 用户级 include 的解析结果必须位于用户配置根目录内。
2. 项目级 include 的解析结果必须位于项目根目录内。
3. 绝对路径、`..` 越界和符号链接越界产生 `instruction_include_outside_root`。
4. 规范化后的同一文件再次出现在当前 include stack 中产生 `instruction_include_cycle`。
5. 入口文件深度为 `0`；允许的最大 include 深度为 `5`，第 `6` 层产生 `instruction_include_depth_exceeded`。
6. 单个读取文件严格大于 `64 KiB` UTF-8 bytes 时产生 `instruction_file_too_large`。
7. 单层展开结果严格大于 `256 KiB` UTF-8 bytes 时产生 `instruction_total_too_large`。

错误只显示入口层、稳定错误码和相对路径；不显示文件正文。

### 6.4 优先级与 Prompt 注入

加载结果最多生成两条 `RuntimeInstruction`，顺序精确为：

```text
runtime.instructions.project
runtime.instructions.user
```

两条消息均为：

```text
kind = "instruction"
scope = "session"
anchor = 0
```

项目级排在用户级之前。`core.runtime_protocol` 同时声明：项目级指令优先于用户级指令；发生冲突时遵循项目级；两层指令都不能覆盖代码层授权、安全限制或当前用户请求范围。

空白入口或 include 展开后只有空白的层不生成消息。普通用户在聊天中输入相同 wrapper 或 identifier 不具备 typed `ControlMessage` 身份。

## 7. 会话目录与标识

会话根目录精确为：

```text
Path.home() / ".mewcode-agent" / "sessions"
```

每个 session ID 是随机 `128` bit 的 32 位小写十六进制字符串：

```text
<sessions_root> / <session_id> /
  messages.jsonl
  meta.json
```

规则：

1. 应用启动时生成当前 session ID，但在第一条消息写入前不创建目录，避免产生空会话。
2. 第一次持久化消息时创建目录、`messages.jsonl` 和 `meta.json`。
3. POSIX 上 session 目录权限为 `0700`，文件权限为 `0600`。
4. Windows 上不声称增加额外 ACL 沙箱。
5. 不创建索引数据库；`/sessions` 只读取各 session 的 `meta.json`。
6. 不进行任何自动删除、过期清理或数量淘汰。

## 8. JSONL 记录契约

### 8.1 行格式

每行是一个完整 UTF-8 JSON object，并以单个 `\n` 结束。根字段顺序精确为：

```json
{
  "schema_version": 1,
  "session_id": "<32hex>",
  "sequence": 1,
  "created_at": "<ISO-8601 with UTC offset>",
  "record_type": "message",
  "message": {}
}
```

只允许 `record_type="message"`。未知 record type、未知键、缺失键或错误类型的行视为坏行。

`message` 字段顺序和结构精确为：

```json
{
  "role": "user|assistant|tool",
  "content": "...",
  "tool_calls": [
    {
      "call_id": "...",
      "name": "...",
      "arguments_json": "..."
    }
  ],
  "tool_call_id": null,
  "thinking_blocks": [
    {
      "text": "...",
      "signature": "..."
    }
  ]
}
```

字段必须满足现有 `ChatMessage`、`ToolCall` 和 `ThinkingBlock` 构造约束，不根据 role 猜测缺失字段。`tool_calls` 和 `thinking_blocks` 即使为空也必须存在；`tool_call_id` 即使为空也必须显式为 `null`。

### 8.2 大小限制

单行上限为 `65 MiB` UTF-8 bytes，包含 JSON envelope 和结尾换行。该上限允许保存 Chapter 06 最大 `64 MiB` 原始 artifact 正文及结构开销。超过上限时拒绝追加并产生 `session_record_too_large`，不截断消息。

本章不设置 session 总大小上限，也不自动删除旧 session。磁盘写入失败时返回稳定错误，不能谎报已保存。

### 8.3 追加与持久化顺序

`ConversationHistory` 新增一个可替换的同步 append recorder。新增消息的固定顺序为：

1. 构造并完整验证不可变 `ChatMessage`；
2. recorder 把完整 JSON line 追加到 `messages.jsonl`；
3. flush 并 `fsync` JSONL；
4. 使用临时文件和原子 replace 更新 `meta.json`；
5. 将同一个 `ChatMessage` 追加到内存列表。

步骤 2–4 失败时不修改内存历史。单进程内使用 lock 串行追加；不支持两个 MewCode 进程同时写同一 session。

`replace_tool_messages()` 不调用 recorder。JSONL 始终保留原始工具结果，当前进程内的 artifact preview 只是可重建投影。

## 9. `meta.json` 契约

根字段顺序精确为：

```json
{
  "schema_version": 1,
  "session_id": "<32hex>",
  "project_root": "<absolute normalized path>",
  "provider_id": "...",
  "model": "...",
  "title": "...",
  "summary": "...",
  "message_count": 0,
  "last_sequence": 0,
  "created_at": "<ISO-8601 with UTC offset>",
  "updated_at": "<ISO-8601 with UTC offset>"
}
```

生成规则：

1. `title` 初始为 `新会话`。
2. 第一条 user message 写入时，取其第一条非空文本行，去除两端空白并截到前 `80` 个 Unicode code points 作为 title。
3. `summary` 初始为空字符串。
4. 每次不带 tool calls 的 assistant message 写入时，把正文中的连续空白折叠为单个 ASCII space，取前 `200` 个 Unicode code points 作为 summary。
5. `message_count` 是 JSONL 中恢复后有效的消息数。
6. `last_sequence` 是最后一个有效 record sequence；空会话为 `0`。
7. `created_at` 创建后不改变，`updated_at` 每次成功追加后更新。

title 和 summary 只用于本地会话列表，不注入 Prompt，不替代普通历史，也不由 LLM 生成。

## 10. 恢复、坏行与修复

### 10.1 扫描规则

恢复按文件物理顺序逐行读取，单行读取必须使用有界 binary `readline()`：

1. 最后一行没有 `\n` 时仍尝试解析；解析失败则跳过。
2. UTF-8、JSON、根 schema、session ID、timestamp 或 message schema 无效时跳过该行并继续。
3. 有效 sequence 必须严格大于此前接受的 sequence；重复或回退的行跳过。
4. sequence 允许出现空洞，不能因为坏行缺号而拒绝后续有效行。
5. 每个坏行只累计脱敏诊断的行号和稳定错误码，不记录原始内容。

扫描诊断码精确为：`session_line_too_large`、
`session_line_missing_newline`、`session_line_invalid_newline`、
`session_line_invalid_utf8`、
`session_line_invalid_json`、`session_line_invalid_schema`、
`session_line_sequence_not_increasing` 和 `session_tool_batch_invalid`。

### 10.2 工具批次完整性

完成逐行解析后，从第一条消息开始验证现有原子历史规则：

1. user 单条完整；
2. 不带 tool calls 的 assistant 单条完整；
3. 带 tool calls 的 assistant 必须紧随数量、顺序和 `tool_call_id` 精确匹配的全部 tool results；
4. 孤立 tool、缺失结果、重复 call ID、乱序结果或中间插入非 tool 消息都使恢复历史截断到该 assistant 或孤立 tool 之前；
5. 工具结构错误之后的所有消息都不恢复，不能根据工具名或位置重新配对。

### 10.3 修复重写

只要发生坏行跳过、无换行尾行、sequence 归一化或工具结构截断，就执行一次异常恢复重写：

1. 把恢复后的完整消息写入同目录临时 JSONL；
2. sequence 重新编号为从 `1` 连续递增；
3. 保留每条接受记录的原 `created_at`；
4. flush、`fsync`、关闭并原子 replace `messages.jsonl`；
5. 根据修复结果原子重建 `meta.json`。

正常运行始终只追加；只有显式恢复修复允许重写 JSONL。修复失败时不打开该会话进行续写。

`meta.json` 缺失、损坏或与恢复结果不一致时，以恢复后的 JSONL 和命令提供的当前项目/Provider 信息重建，不扫描其他 session 内容。

## 11. 恢复后的上下文处理

`/resume` 只允许在没有活动 Agent run、手动压缩或笔记清空确认时执行。精确顺序为：

1. 保存并关闭当前 session journal；
2. 读取并修复目标 JSONL；
3. 用恢复消息替换普通历史；
4. 绑定目标 session journal；
5. 重置 Prompt 和 Context manager 的会话状态；
6. 重新加载当前指令和当前笔记 session controls；
7. Layer 1 重新外置恢复出的超大原始 tool results；
8. 估算完整 Provider 请求；
9. 仅当估值不小于 `prompt_budget_tokens` 时，自动发起一次恢复压缩；
10. 重绘 TUI 转录。

恢复压缩使用现有无工具摘要事务。失败时会话仍保持已恢复状态，但下一次普通请求必须继续遵守 `context_window_exceeded`，不能发送已知超预算请求。

### 11.1 时间跨度提醒

如果当前时间与 meta 的 `updated_at` 相差至少 `7 * 24 * 60 * 60` 秒，注入一条代码生成的 session context：

```text
instruction_id = runtime.session.resume_gap
kind = context
scope = session
```

正文精确包含上次活跃时间、当前时间和完整天数，并说明项目文件、依赖、分支和外部状态可能已经变化，需要重新读取后再下结论。该消息不写入普通历史或 JSONL。

## 12. 会话命令

只在去除输入首尾空白后按以下精确形式识别：

| 命令 | 行为 |
| --- | --- |
| `/sessions` | 列出 `project_root` 与当前项目精确相等的 session meta，按 `updated_at` 降序、session ID 升序 |
| `/resume <session_id>` | 恢复精确 32 位小写十六进制 session ID |
| `/session path <session_id>` | 显示目标 session 目录的精确绝对路径 |
| `/session delete <session_id>` | 弹出确认界面，确认后删除非活动 session |

大小写不同、参数缺失、参数多余或 session ID 格式不同的输入作为普通用户消息，不猜测命令意图。

删除规则：

1. 当前活动 session 不能删除，返回 `session_delete_active`。
2. 只删除根目录下精确匹配且 meta 内 session ID 一致的普通目录。
3. 路径越界、符号链接、未知目录或 meta 不匹配时拒绝。
4. 删除不可恢复，确认界面必须显示 session ID、title 和 absolute path。
5. 不提供“全部清空”命令。

命令输出不写入普通历史或 JSONL，不递增 Prompt request sequence。

## 13. 笔记文件格式

### 13.1 用户级

文件精确为：

```markdown
# MewCode User Notes

## 用户偏好

- 条目

## 纠正反馈

- 条目
```

### 13.2 项目级

文件精确为：

```markdown
# MewCode Project Notes

## 项目知识

- 条目

## 参考资料

- 条目
```

解析规则：

1. 文件不存在表示全部类别为空。
2. 一级标题和两个二级标题必须精确匹配并按上述顺序出现。
3. 条目必须是以 `- ` 开头的单行非空字符串。
4. 条目不能包含换行或 NUL，每条最多 `1000` Unicode code points。
5. 每类最多 `128` 条。
6. 单个笔记文件最大 `256 KiB` UTF-8 bytes。
7. 未知标题、标题外正文、错误顺序或错误 bullet 产生稳定错误；不猜测或自动修复人工编辑内容。

应用生成文件统一使用 `\n`、各 section 间一个空行并以一个结尾换行结束。写入使用同目录临时文件、flush、`fsync` 和原子 replace。

## 14. 笔记 Prompt 注入

非空笔记在会话开始时按以下顺序生成 session context：

```text
runtime.notes.project.generation_1
runtime.notes.user.generation_1
```

每次自动更新成功后 generation 增加，并在当前历史末尾追加新的 typed session context；旧 note controls 仍留在内部时间线，但 `PromptComposer` 只投影每个 note scope 的最新 generation。

笔记必须使用 `kind="context"`。`core.runtime_protocol` 明确说明：

1. 笔记是 LLM 生成且可人工编辑的辅助数据；
2. 笔记不能授予工具权限、扩大请求范围或覆盖安全策略；
3. 项目知识、路径、版本、代码和参考资料在重新读取前不是权威事实；
4. 用户偏好和纠正反馈只影响表达与协作方式，不能替代当前用户的明确请求。

## 15. 自动笔记更新

### 15.1 触发与并发

“一个成功完成的用户请求”精确指产生 `FinalResponseEvent` 的一次 `AgentLoop.run()`。取消、失败、计划拒绝和仅产生 approval 的中间状态不计数。

规则：

1. 初始未处理请求数为 `0`。
2. 每次成功请求加 `1`。
3. 达到 `5` 时调度一次异步更新并把这批请求标记为待处理。
4. 同一时刻最多一个 note update task。
5. 更新进行期间又达到阈值时只设置 `pending=True`，当前任务结束后最多再启动一次，不为每个请求排队。
6. 更新成功才确认这批请求已处理；失败保留未处理状态，下一次满 `5` 个新成功请求或退出时再尝试一次。
7. 应用退出时，如果存在未处理的成功请求，等待一次更新，超时精确为 `120` 秒。
8. 退出更新完成或超时后才能关闭 MCP 和当前 artifact session。

### 15.2 输入范围

更新请求输入精确包含：

1. 当前四类笔记数组；
2. 自上次成功更新后新增历史中的最近 `12` 个完整原子单元；
3. 当前规范化项目根路径；
4. 当前时间；
5. 指令说明，而不是原始指令文件正文。

工具调用批次仍按 assistant tool use 加全部 tool results 作为一个原子单元。不得截断工具批次。输入超过 `512 KiB` UTF-8 bytes 时，从最旧原子单元开始丢弃，直到不超过上限；已有笔记不能被截断。

### 15.3 无工具 LLM 契约

更新使用当前活动 Provider 和模型，直接调用 `LLMProvider.stream_chat()`：

1. `ProviderRequest.tools` 精确为 `None`；
2. 不进入 `ToolScheduler`；
3. 不写入普通历史或 session JSONL；
4. 不产生普通 assistant/thinking 转录；
5. System Prompt 第一行和最后一行都精确禁止调用工具；
6. 任何 `ProviderToolCall` 产生 `notes_tool_call_forbidden`；
7. response 正文最大 `256 KiB` UTF-8 bytes；
8. 只接受 `end_turn`，拒绝 `max_tokens`、`tool_calls`、未知 stop reason、缺失或重复 usage；
9. usage 使用独立 `NoteUsageRecord`，`request_kind="notes"`，不伪装成 Agent round 或 compaction usage。

根 JSON 字段顺序精确为：

```json
{
  "analysis_draft": [],
  "notes": {
    "user_preferences": [],
    "correction_feedback": [],
    "project_knowledge": [],
    "references": []
  }
}
```

`analysis_draft` 只列事实覆盖、冲突和去重检查，解析后丢弃。`notes` 的四个键和顺序必须精确匹配，值必须满足笔记条目上限。LLM 负责合并、更新和语义去重；代码只做结构、大小和类型校验，不计算 embedding、编辑距离或关键词相似度。

用户级和项目级文件各自独立事务写入。某一层写入失败时保留该层旧文件；另一层已经成功的更新保持有效，并产生脱敏 warning。

## 16. 笔记命令

只识别以下精确命令：

| 命令 | 行为 |
| --- | --- |
| `/notes` | 显示四类当前笔记，不写入历史 |
| `/notes paths` | 显示用户级和项目级笔记的精确绝对路径 |
| `/notes clear user` | 确认后原子写入空的用户级标准 Markdown |
| `/notes clear project` | 确认后原子写入空的项目级标准 Markdown |

大小写不同、参数缺失或多余文本按普通用户消息处理。clear 确认界面必须显示 scope 和 absolute path；取消时不修改文件。清空成功后注入新的空 note generation，使后续 Provider 请求不再看到旧 note control。

## 17. 生命周期顺序

应用启动顺序精确为：

```text
解析 session environment 和 project_root
→ 加载用户/项目指令
→ 加载用户/项目笔记
→ 创建 PromptRuntime session controls
→ 创建当前 lazy session
→ 创建 Provider、artifact store、ToolRegistry、MCP 和 AgentLoop
→ 启动 Textual
```

正常退出顺序精确为：

```text
停止接受新 Agent run
→ 等待或超时终止 notes flush
→ 关闭当前 session journal
→ 关闭 MCP manager
→ 清理当前 artifact session
```

启动任一阶段失败时关闭已经打开的 journal、MCP 和 artifact store。不得留下 note task、JSONL writer 或 Provider stream。

## 18. 稳定错误码

### 18.1 指令

| 错误码 | 含义 |
| --- | --- |
| `instruction_read_failed` | 入口或 include 文件无法读取 |
| `instruction_invalid_utf8` | 文件不是有效 UTF-8 |
| `instruction_file_too_large` | 单文件超过 `64 KiB` |
| `instruction_total_too_large` | 单层展开结果超过 `256 KiB` |
| `instruction_include_invalid` | directive 语法或 path 无效 |
| `instruction_include_not_found` | include 目标不存在或不是普通文件 |
| `instruction_include_outside_root` | include 越过所属根目录 |
| `instruction_include_cycle` | include stack 出现循环 |
| `instruction_include_depth_exceeded` | include 深度超过 `5` |

### 18.2 会话

| 错误码 | 含义 |
| --- | --- |
| `session_write_failed` | JSONL 或 meta 持久化失败 |
| `session_record_too_large` | 单行超过 `65 MiB` |
| `session_not_found` | session ID 未找到 |
| `session_invalid_meta` | meta 无法验证且不能安全重建 |
| `session_repair_failed` | JSONL 异常恢复重写失败 |
| `session_access_denied` | session 路径不属于精确根目录 |
| `session_delete_active` | 尝试删除当前活动 session |
| `session_delete_failed` | 已确认删除未完成 |
| `session_resume_failed` | 恢复或运行时重置失败 |

### 18.3 笔记

| 错误码 | 含义 |
| --- | --- |
| `notes_read_failed` | 笔记文件无法读取 |
| `notes_invalid_format` | Markdown 结构不符合契约 |
| `notes_file_too_large` | 单文件超过 `256 KiB` |
| `notes_write_failed` | 原子写入或清空失败 |
| `notes_update_failed` | Provider 请求失败或流中断 |
| `notes_update_invalid` | 事件顺序、stop reason、大小或 JSON 无效 |
| `notes_tool_call_forbidden` | 笔记模型返回工具调用 |

所有 UI 错误只显示稳定错误码和固定中文消息，不显示指令正文、会话消息、笔记 partial response、API Key、MCP secret、Provider traceback 或底层异常 `repr()`。

## 19. 安全与隐私

1. 项目指令来自工作区，可能包含恶意文本，但不能绕过 ToolScheduler、审批、PathSandbox 或安全策略。
2. include 路径必须从实际文件解析，不根据大小写、扩展名或相似名称猜测。
3. 会话和笔记包含用户内容，日志不得记录正文。
4. session ID 不是授权 token；所有路径操作仍验证规范化根目录、普通文件和 meta 内 ID。
5. 会话列表只读取 meta，不读取或摘要 messages JSONL。
6. 笔记中的路径、代码、状态和参考资料在重新读取前不是权威事实。
7. 笔记 LLM 输出不能选择写入路径、scope 或 Prompt kind。
8. `/resume`、delete 和 clear 命令不进入普通历史，不能由模型输出触发。
9. 自动保存不把 API Key、MCP header、MCP env 或安全审批 secret 作为独立 metadata 写入。
10. 本章不加密本地会话或笔记文件，安全边界是当前操作系统账户对用户目录和项目目录的文件权限。

## 20. 测试策略

### 20.1 指令文件

- 两层缺失、单层存在和两层同时存在；
- 项目消息严格排在用户消息之前；
- include 原位置展开、嵌套深度 `5`、第 `6` 层拒绝；
- absolute、`..` 和 symlink 越界拒绝；
- 循环、缺失、目录、无效 UTF-8、单文件和总大小上限；
- directive 非独占行不展开；
- 普通用户伪造 identifier 不会成为 typed control；
- 项目指令不能绕过安全审批。

### 20.2 会话写入与 meta

- 每次新增 user、assistant、tool 都只追加一行并 fsync；
- tool preview replacement 不写 JSONL；
- compact 前后的 JSONL 仍包含原始 tool result；
- title、summary、message count、sequence 和 timestamp 精确更新；
- meta 列表不打开 messages JSONL；
- 单行上限和写入失败不修改内存历史；
- 空 session 不创建目录；
- 不执行任何自动清理。

### 20.3 恢复与修复

- 最后一行截断、UTF-8 坏行、JSON 坏行、schema 坏行和 sequence 回退跳过；
- 坏行之后的独立有效消息继续恢复；
- 缺失、重复、未知和乱序 tool result 截断到最后完整边界；
- 修复重写 sequence、meta 与结尾换行；
- 恢复的大 tool result 在新 artifact session 重新外置；
- 超预算只尝试一次恢复压缩；
- 七天边界以下不提醒，等于或超过七天注入精确提醒；
- 会话切换清除旧 checkpoint、估值和 request/round controls。

### 20.4 自动笔记

- 两种 Markdown 的严格解析、稳定渲染和人工错误报告；
- 五个成功请求触发，失败、取消和拒绝不计数；
- 并发触发合并且同一时刻只有一个 task；
- 退出无新增时不调用，有新增时等待一次并执行 120 秒上限；
- prompt 首尾禁止工具且 `tools is None`；
- tool call、事件乱序、缺失 usage、错误 stop reason、超大正文和错误 JSON 被拒绝；
- draft 丢弃，四类键顺序和 scope 分流精确；
- 去重没有本地相似度代码；
- 笔记更新不进入普通历史或 session JSONL；
- project/user 写入独立失败边界；
- 新 generation 替代旧 note 投影且不获得授权。

### 20.5 命令与生命周期

- 精确命令不进入历史、不递增 request sequence；
- 大小写、参数缺失和多余参数按普通消息处理；
- session/notes path 返回精确路径；
- delete/clear 必须确认，取消零修改；
- 活动 session 不可删除；
- 启动失败和正常退出关闭 journal、note task、MCP 与 artifact；
- Chapter 01–06 全部默认测试继续通过。

## 21. 验收标准

1. 新会话的第一条真实用户消息之前存在代码生成的项目/用户指令独立消息，项目级严格排在用户级之前。
2. include 只能读取所属根目录内的精确相对路径，并受到深度、循环、单文件和总大小限制。
3. 每条普通历史消息以一行 JSONL 追加，崩溃造成的不完整尾行不会阻止恢复其他有效记录。
4. 会话列表只读 meta，不扫描完整 JSONL。
5. JSONL 保存原始工具结果，不持久化会在退出时失效的 artifact preview。
6. 恢复不会产生孤立 tool result；不完整工具批次之后的历史不会被错误保留。
7. 恢复修复后可以继续追加，后续再次恢复不会重复命中同一坏尾部。
8. 超预算恢复先尝试一次上下文压缩，失败时不发送已知超预算请求。
9. 七天或更久的恢复会话收到非授权型时间跨度提醒。
10. 会话不发生任何自动清理；删除只能由精确命令、非活动目标和用户确认共同触发。
11. 自动笔记按五个成功请求和退出未处理请求触发，同一时刻最多一个更新任务。
12. 笔记更新无工具、无 scheduler、无普通转录，并使用严格 JSON 和固定大小上限。
13. 用户偏好/纠正反馈与项目知识/参考资料进入各自精确文件，代码不实现相似度去重。
14. 笔记以 context 而不是 instruction 注入，不能改变工具权限或安全边界。
15. `/sessions`、`/resume`、`/session path`、`/session delete`、`/notes`、`/notes paths` 和 `/notes clear` 的精确形式均可测试。
16. 所有磁盘、解析、恢复、LLM、删除、清空和退出路径都有稳定脱敏错误与固定资源上限。

## 22. 固定初始参数

| 参数 | 值 |
| --- | ---: |
| 指令 include 最大深度 | `5` |
| 单指令文件上限 | `64 KiB` UTF-8 bytes |
| 单层指令合并上限 | `256 KiB` UTF-8 bytes |
| session ID | 随机 `128` bit，32 位小写十六进制 |
| JSONL 单行上限 | `65 MiB` UTF-8 bytes |
| title 上限 | `80` Unicode code points |
| meta summary 上限 | `200` Unicode code points |
| 会话自动清理 | 禁用 |
| 恢复时间跨度提醒 | `7` 天 |
| 自动笔记触发间隔 | `5` 个成功用户请求 |
| 笔记最近历史 | `12` 个完整原子单元 |
| 笔记输入历史上限 | `512 KiB` UTF-8 bytes |
| 单笔记文件上限 | `256 KiB` UTF-8 bytes |
| 单类别条目上限 | `128` 条 |
| 单条笔记上限 | `1000` Unicode code points |
| 笔记响应上限 | `256 KiB` UTF-8 bytes |
| 退出笔记更新超时 | `120` 秒 |
