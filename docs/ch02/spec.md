# Chapter 02 Specification: ReAct Agent 循环与事件流

## 1. 文档状态

- 状态：设计已逐项确认，等待用户审核本文档
- 实现授权：尚未授权
- 前置实现：Chapter 01 多轮对话、双协议 Provider、工具注册与执行
- 本章目标：把 ReAct Agent 循环从 Textual UI 中抽离，通过类型化异步事件流暴露执行过程，并加入工具调度、plan-only、审批、取消和超时边界。

本文档审核通过后，再创建：

```text
docs/ch02/plan.md
docs/ch02/tasks.md
docs/ch02/checklist.md
```

## 2. 已确认的技术决策

| 项目 | 决策 |
| --- | --- |
| Agent 范式 | ReAct：LLM → 工具 → 结果回填 → 下一轮 |
| 核心与 UI | `AgentLoop` 完全独立于 Textual |
| 输出接口 | `AsyncIterator[AgentEvent]` |
| 反向控制 | `AgentRunContext` |
| 单次请求最大 LLM 轮数 | `15` |
| 单轮 LLM 超时 | `120` 秒 |
| 单工具默认超时 | 保留现有 `30` 秒 |
| 工具分类 | `Literal["read", "write", "command"]` |
| 并发规则 | 连续读工具并发，写与命令工具作为串行屏障 |
| plan-only 默认状态 | 关闭；用户开启后跨请求保持，直到用户关闭 |
| plan-only 单工具授权 | 只允许当前一次工具调用 |
| 最终计划授权 | 只允许当前用户请求，后续请求仍保持 plan-only |
| 规划与执行计数 | 共用同一个 `15` 轮计数器，不重新计数 |
| thinking | 只透传 Provider 的真实分片，不生成、不写入历史 |
| 用户审批超时 | 不设置；一直等待选择或取消 |
| 工具执行中取消 | 等待已经启动的工具完成或超时 |
| 权限策略 | 本章只预留前后拦截接口，不实现规则系统 |

## 3. 术语约定

### 3.1 用户请求

“用户请求”指用户提交一条消息后，从消息进入历史开始，到最终回复、错误或取消为止的完整运行过程。

同一会话可以包含任意数量的用户请求。`15` 轮限制不会限制整个会话的对话次数。

### 3.2 Agent 轮

每调用一次 `LLMProvider.stream_chat()` 计为一轮。例如：

```text
第 1 轮 LLM → read_file
工具结果回填
第 2 轮 LLM → search_code
工具结果回填
第 3 轮 LLM → 最终正文
请求结束
```

### 3.3 plan-only

`plan_only` 是运行模式开关，不是工具或 Provider 协议字段。

- `False`：正常执行模式。
- `True`：默认只允许读工具，写和命令工具需要用户审批。

plan-only 开关由上层持有。批准单个工具或最终计划不会永久关闭该开关。

### 3.4 当前请求临时授权

用户批准最终计划后，当前请求获得临时执行授权。该请求后续的写和命令工具不再逐次审批。请求进入任意终止状态后，授权立即失效。

## 4. 项目范围

### 4.1 范围内

1. 把现有 Agent 循环从 `ChatApp` 抽离为协议无关、UI 无关的 `AgentLoop`。
2. 通过 `AsyncIterator[AgentEvent]` 输出用户消息、轮次、thinking、正文、审批、工具和终止事件。
3. 使用 `AgentRunContext` 接收工具审批、计划审批和取消信号。
4. Provider 返回结构化的 thinking、正文、完整工具调用和停止原因。
5. OpenAI 与 Anthropic Provider 归一化各自的流式响应。
6. 为工具增加 `read`、`write`、`command` 三类元数据。
7. 连续读工具并发执行，写与命令工具严格串行。
8. 工具结果按模型原始调用顺序回填历史。
9. 支持 plan-only 单工具审批和最终计划审批。
10. 支持当前请求临时执行授权，授权不影响后续请求。
11. 支持外部取消、单轮 LLM 超时和现有工具超时。
12. 保证取消或错误后历史仍满足工具调用协议要求。
13. 在工具执行前后提供默认无操作的拦截接口。
14. Textual UI 消费 Agent 事件并显示两类审批卡片。
15. 使用最小系统提示词区分执行、规划、临时授权和最终轮。

### 4.2 范围外

1. 复杂系统提示词拼装、动态角色模板和 Prompt 插件。
2. 完整权限规则、路径规则、危险命令识别和规则持久化。
3. Agent 作为工具递归调用、子任务委派和多 Agent。
4. 计划文件持久化、计划版本管理和跨进程恢复。
5. 对话持久化、上下文压缩、Token 预算和记忆系统。
6. 工具在 LLM 流尚未结束时提前执行。
7. 强杀正在 `asyncio.to_thread` 中运行的文件或命令线程。
8. 本章未明确列出的后续能力。

## 5. 总体架构

新增独立 Agent 包：

```text
src/mewcode_agent/agent/
├── __init__.py
├── context.py
├── events.py
├── loop.py
└── tool_scheduler.py
```

职责划分：

| 文件 | 职责 |
| --- | --- |
| `agent/context.py` | 审批请求等待、审批结果提交和取消状态 |
| `agent/events.py` | Agent 对外事件、审批选择和运行状态类型 |
| `agent/loop.py` | ReAct 状态机、历史提交、轮数和终止条件 |
| `agent/tool_scheduler.py` | 工具分批、并发读、串行屏障和结果排序 |
| `providers/base.py` | Provider 统一流事件、停止原因和 Protocol |
| `tools/base.py` | `ToolCategory` 和工具元数据 |
| `app.py` | 消费事件、渲染 TUI、展示卡片并提交选择 |
| `cli.py` | 组装 Provider、History、ToolRegistry 和 AgentLoop |

依赖方向：

```text
ChatApp / CLI
    → AgentLoop
        → LLMProvider
        → ConversationHistory
        → ToolScheduler
            → ToolRegistry
        → AgentRunContext
```

`AgentLoop`、`AgentRunContext`、事件模型和调度器不得导入 Textual。

## 6. 核心接口

### 6.1 `AgentLoopConfig`

```python
@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    max_rounds: int = 15
    llm_timeout_seconds: float = 120.0
```

两个字段都必须大于 `0`。

### 6.2 `AgentLoop`

```python
class AgentLoop:
    def run(
        self,
        user_message: str,
        history: ConversationHistory,
        *,
        plan_only: bool,
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]: ...
```

规则：

- `user_message` 去除首尾空白后必须非空。
- 一个 `AgentRunContext` 只服务一次 `run()`。
- 同一个 `AgentLoop` 不保存跨请求的 plan-only 临时授权。
- `ConversationHistory` 是本次运行唯一的消息历史来源。

### 6.3 `AgentRunContext`

```python
ToolApprovalDecision = Literal["allow_once", "reject"]
PlanApprovalDecision = Literal[
    "execute_current",
    "request_changes",
    "reject",
]

class AgentRunContext:
    def cancel(self) -> None: ...

    def resolve_tool_approval(
        self,
        request_id: str,
        decision: ToolApprovalDecision,
    ) -> None: ...

    def resolve_plan_approval(
        self,
        request_id: str,
        decision: PlanApprovalDecision,
        *,
        feedback: str = "",
    ) -> None: ...
```

规则：

- `request_id` 是非空、不透明且只使用一次的标识。
- 未知、过期或已完成的 `request_id` 必须抛出 `ValueError`。
- `request_changes` 必须带非空 `feedback`。
- 其他计划选择不得携带非空 `feedback`。
- `cancel()` 可以重复调用，重复调用无额外效果。
- Context 内部可以使用 `asyncio.Event` 和 `Future`，但不得通过事件向 UI 暴露 `Future`。

## 7. Provider 结构化流

### 7.1 Provider 事件

```python
@dataclass(frozen=True, slots=True)
class ProviderThinkingDelta:
    text: str

@dataclass(frozen=True, slots=True)
class ProviderTextDelta:
    text: str

@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    tool_call: ToolCall

ProviderStopReason = Literal[
    "end_turn",
    "tool_calls",
    "max_tokens",
    "other",
]

@dataclass(frozen=True, slots=True)
class ProviderTurnEnd:
    stop_reason: ProviderStopReason

ProviderStreamEvent = (
    ProviderThinkingDelta
    | ProviderTextDelta
    | ProviderToolCall
    | ProviderTurnEnd
)
```

### 7.2 Provider 接口

```python
class LLMProvider(Protocol):
    @property
    def protocol(self) -> ProviderProtocol: ...

    def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str,
    ) -> AsyncIterator[ProviderStreamEvent]: ...
```

Provider 必须：

- 只输出 Provider 统一事件，不泄露 SDK 对象。
- 只在 API 实际提供 thinking 时输出 `ProviderThinkingDelta`。
- 在工具参数流结束后输出完整 `ProviderToolCall`。
- 每轮恰好输出一个 `ProviderTurnEnd`，并且它必须是最后一个事件。
- 把原始协议停止原因映射到统一 `ProviderStopReason`。
- 把 SDK 异常转换成现有脱敏 `ProviderError`。

thinking 分片只用于输出事件，不写入 `ConversationHistory`，也不发送到下一轮。

## 8. Agent 对外事件

所有事件使用不可变、带 slots 的 dataclass。

```python
@dataclass(frozen=True, slots=True)
class UserMessageEvent:
    content: str

@dataclass(frozen=True, slots=True)
class RoundStartedEvent:
    round_number: int
    max_rounds: int
    mode: Literal["planning", "executing"]

@dataclass(frozen=True, slots=True)
class ModelThinkingEvent:
    text: str

@dataclass(frozen=True, slots=True)
class ModelTextEvent:
    text: str

@dataclass(frozen=True, slots=True)
class ToolApprovalRequestedEvent:
    request_id: str
    call_id: str
    tool_name: str
    arguments_json: str
    category: ToolCategory

@dataclass(frozen=True, slots=True)
class PlanApprovalRequestedEvent:
    request_id: str
    plan: str
    can_execute: bool
    can_request_changes: bool

@dataclass(frozen=True, slots=True)
class ToolCallStartedEvent:
    call_id: str
    tool_name: str
    arguments_json: str
    category: ToolCategory

@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    call_id: str
    result: ToolResult

@dataclass(frozen=True, slots=True)
class FinalResponseEvent:
    content: str
    total_rounds: int

@dataclass(frozen=True, slots=True)
class RunErrorEvent:
    code: str
    message: str

@dataclass(frozen=True, slots=True)
class RunCancelledEvent:
    reason: str
```

```python
AgentEvent = (
    UserMessageEvent
    | RoundStartedEvent
    | ModelThinkingEvent
    | ModelTextEvent
    | ToolApprovalRequestedEvent
    | PlanApprovalRequestedEvent
    | ToolCallStartedEvent
    | ToolResultEvent
    | FinalResponseEvent
    | RunErrorEvent
    | RunCancelledEvent
)
```

终止事件 `FinalResponseEvent`、`RunErrorEvent` 和 `RunCancelledEvent` 互斥。一次运行只能产生其中一个，且产生后不得再输出其他事件。

## 9. Agent 状态机

```python
AgentRunState = Literal[
    "planning",
    "waiting_tool_approval",
    "waiting_plan_approval",
    "executing",
    "completed",
    "cancelled",
    "failed",
]
```

状态转换：

```text
START
  ├─ plan_only=False → executing
  └─ plan_only=True  → planning

planning
  ├─ 读工具 → 执行 → planning
  ├─ 写/命令 → waiting_tool_approval
  └─ 无工具 → waiting_plan_approval

waiting_tool_approval
  ├─ allow_once → 执行当前工具 → planning
  ├─ reject → 回填拒绝结果 → planning
  └─ cancel → cancelled

waiting_plan_approval
  ├─ execute_current → executing
  ├─ request_changes → planning
  ├─ reject → cancelled
  └─ cancel → cancelled

executing
  ├─ 有工具 → 调度 → executing
  ├─ 无工具且正文非空 → completed
  ├─ cancel → cancelled
  └─ 终止错误 → failed
```

## 10. ReAct 循环

### 10.1 请求开始

1. 校验用户消息。
2. 将原始用户消息写入历史。
3. 发出 `UserMessageEvent`。
4. 根据 `plan_only` 进入 `planning` 或 `executing`。
5. 初始化轮数为 `0`，当前请求临时授权为 `False`。

### 10.2 每轮模型调用

1. 轮数加一。
2. 发出 `RoundStartedEvent`。
3. 构造本轮最小系统提示词。
4. 第 `1–14` 轮可以传递工具定义。
5. 第 `15` 轮必须传递 `tools=None` 并加入强制收尾提示。
6. 使用 `120` 秒 Agent 层超时消费完整 Provider 流。
7. thinking 和正文分片立即转换为 Agent 事件。
8. 在 `ProviderTurnEnd` 前只收集内容，不执行工具。

### 10.3 有工具调用

1. 如果当前是第 `15` 轮，终止为 `max_rounds_exceeded`，不得执行工具。
2. 把完整 assistant 正文和工具调用写入历史。
3. 把工具列表交给 `ToolScheduler`。
4. 按原始调用顺序收集全部工具结果。
5. 把每个工具结果写入历史。
6. 如果没有取消，进入下一轮。

### 10.4 无工具调用

执行状态：

- 正文非空：写入 assistant 历史，发出 `FinalResponseEvent`。
- 正文为空：终止为 `empty_response` 或 `invalid_provider_stream`。

规划状态：

1. 正文必须非空并作为计划写入 assistant 历史。
2. 发出 `PlanApprovalRequestedEvent`。
3. 等待 `AgentRunContext`。
4. `execute_current`：追加内部 user 控制消息 `计划已批准，请执行当前计划。`，设置当前请求临时授权并进入执行状态。
5. `request_changes`：把非空反馈作为 user 消息写入历史，发出对应 `UserMessageEvent`，继续规划。
6. `reject`：发出 `RunCancelledEvent(reason="plan_rejected")`。

内部批准控制消息不产生 `UserMessageEvent`，TUI 只显示计划已批准的状态。

### 10.5 第 15 轮计划

如果规划状态直到第 `15` 轮才产出计划：

- `PlanApprovalRequestedEvent.can_execute` 为 `False`。
- `PlanApprovalRequestedEvent.can_request_changes` 为 `False`。
- TUI 禁用执行和修改选项。
- 卡片提示：`当前请求已达到 15 轮上限，请开启新请求执行该计划。`
- 用户结束卡片后，本次请求以 `RunCancelledEvent(reason="round_limit_after_plan")` 终止。

规划和执行共用轮数，批准计划后不得重置。

## 11. 工具分类与调度

### 11.1 分类

```python
ToolCategory = Literal["read", "write", "command"]
```

| 工具 | `category` |
| --- | --- |
| `read_file` | `read` |
| `find_files` | `read` |
| `search_code` | `read` |
| `write_file` | `write` |
| `edit_file` | `write` |
| `run_command` | `command` |

### 11.2 分批

```text
read1, read2, write1, read3, command1
```

形成：

```text
[并发 read1 + read2]
[串行 write1]
[单独 read3]
[串行 command1]
```

未知工具在原始位置生成 `tool_not_found`，并作为调度屏障，不得跨越它重排其他调用。

### 11.3 执行

- 连续读批次使用 `asyncio.gather()`。
- `ToolCallStartedEvent` 按原始顺序发出。
- 并发结果按原始调用顺序输出和写入历史，不按完成时间排序。
- `write` 和 `command` 每次只执行一个。
- 工具失败只产生失败 `ToolResultEvent`，不会直接终止 Agent。
- 每个工具继续由 `ToolRegistry.execute()` 应用自己的超时和结构化错误处理。

## 12. plan-only 与审批

### 12.1 单工具审批

规划状态遇到 `write` 或 `command`：

1. 发出 `ToolApprovalRequestedEvent`。
2. 暂停该工具，等待用户选择。
3. `allow_once`：只执行当前 `call_id`，plan-only 保持不变。
4. `reject`：不发出 `ToolCallStartedEvent`，生成失败工具结果：

```text
error_code = "tool_blocked_in_plan_mode"
error_message = "工具在 plan-only 模式下被用户拒绝"
```

同一模型响应包含多个写或命令工具时，每个工具分别审批。

### 12.2 最终计划审批

卡片提供：

1. `执行当前计划`
2. `修改计划`
3. `拒绝并结束`

批准后：

- 当前请求临时授权设为 `True`。
- 当前请求后续写和命令工具全部免除逐次审批。
- plan-only UI 开关仍保持开启。
- 本次请求结束后临时授权失效。
- 下一条用户消息重新进入规划状态。

## 13. 工具执行拦截接口

```python
class ToolExecutionInterceptor(Protocol):
    async def before_execute(
        self,
        tool_call: ToolCall,
        *,
        plan_only: bool,
        current_request_authorized: bool,
    ) -> ToolResult | None: ...

    async def after_execute(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> ToolResult: ...
```

本章提供默认无操作实现：

- `before_execute()` 返回 `None`。
- `after_execute()` 原样返回 `result`。

plan-only 审批是本章 Agent 状态机的明确功能，不由默认拦截器实现。完整权限策略在后续章节实现。

## 14. 历史一致性

### 14.1 正常路径

- 原始用户消息在首次 LLM 调用前写入历史。
- assistant 工具调用消息必须在对应工具结果之前写入。
- 每个工具结果必须使用准确的 `tool_call_id`。
- 多工具结果必须按模型原始调用顺序写入。
- 最终正文必须在发出 `FinalResponseEvent` 前写入 assistant 历史。
- thinking 不写入历史。

### 14.2 流失败或取消

- Provider 流未完整结束时，不写入该轮 assistant 消息。
- 已经输出到 UI 的正文和 thinking 分片视为临时展示。
- Provider 错误文字不得加入模型历史。

### 14.3 工具批次取消

assistant 工具调用消息一旦进入历史，每个 `tool_call_id` 都必须获得结果：

- 已启动工具：等待成功、失败或超时，并写入真实结果。
- 未启动工具：写入失败结果：

```text
error_code = "tool_cancelled"
error_message = "工具因用户取消而未执行"
```

全部结果写入后才发出 `RunCancelledEvent`。

## 15. 取消与超时

### 15.1 取消

- 等待 LLM：立即停止消费并尝试关闭异步流。
- 等待工具审批或计划审批：立即结束等待。
- 工具已经启动：等待该工具完成或达到工具超时。
- 并发读批次已经启动：等待该批次全部已启动读工具完成或超时。
- 取消后不得启动后续工具或下一轮 LLM。
- 取消终止只发出 `RunCancelledEvent`。

### 15.2 超时

| 边界 | 超时 |
| --- | --- |
| 单轮 LLM | `120` 秒 |
| 单工具 | 工具的 `timeout_seconds`，当前默认 `30` 秒 |
| 用户审批 | 无超时 |
| 整个请求 | 无总时长超时，由 `15` 轮限制 |

LLM 超时后未完成正文不写入历史。

## 16. 错误模型

Agent 终止错误：

| 错误码 | 条件 |
| --- | --- |
| `provider_error` | Provider 返回脱敏 `ProviderError` |
| `llm_timeout` | 单轮 LLM 超过 `120` 秒 |
| `max_tokens_reached` | Provider 以 `max_tokens` 停止 |
| `max_rounds_exceeded` | 第 15 轮在 `tools=None` 时仍返回工具调用 |
| `empty_response` | 没有正文、thinking 或工具调用 |
| `invalid_provider_stream` | 缺少结尾事件、结尾事件不在最后、只有 thinking 没有正文，或停止原因与内容冲突 |

工具错误通过 `ToolResultEvent` 返回：

```text
tool_not_found
invalid_json
invalid_arguments
timeout
file_not_found
permission_denied
tool_blocked_in_plan_mode
tool_cancelled
```

终止错误使用 `RunErrorEvent`，其消息必须适合直接展示且不得包含 API Key、完整请求头或 SDK 原始对象。

## 17. 最小系统提示词

本章不实现复杂提示词组装，只根据运行状态组合以下准确片段。

普通执行：

```text
You are a coding agent. Use the available tools when needed.
When the task is complete, return a final response without tool calls.
```

规划：

```text
You are in plan-only mode. Inspect the project with read tools and produce
an implementation plan. Write and command tools require user approval.
```

最终计划获批：

```text
The user approved the current plan. Execute it for this request.
The approval expires when this request ends.
```

第 15 轮强制收尾：

```text
This is the final allowed model round. Do not request tools.
Return the best final response using the available results.
```

## 18. TUI 集成

`ChatApp` 不再实现 ReAct 循环，只执行：

1. 接收用户输入。
2. 创建本次请求的 `AgentRunContext`。
3. 调用 `AgentLoop.run()`。
4. 消费并渲染 `AgentEvent`。
5. 把卡片选择写回 `AgentRunContext`。
6. 把 Escape 或取消操作转换为 `context.cancel()`。

工具审批卡片：

```text
仅本次允许
拒绝
```

最终计划审批卡片：

```text
执行当前计划
修改计划
拒绝并结束
```

第 15 轮计划卡片禁用前两个选项，只允许结束，并显示轮数提示。

## 19. 文件结构

本章完成后的相关结构：

```text
docs/
└── ch02/
    ├── spec.md
    ├── plan.md
    ├── tasks.md
    └── checklist.md
src/mewcode_agent/
├── agent/
│   ├── __init__.py
│   ├── context.py
│   ├── events.py
│   ├── loop.py
│   └── tool_scheduler.py
├── app.py
├── cli.py
├── history.py
├── models.py
├── providers/
│   ├── base.py
│   ├── openai_provider.py
│   └── anthropic_provider.py
└── tools/
    ├── base.py
    └── registry.py
tests/
├── test_agent_context.py
├── test_agent_events.py
├── test_agent_loop.py
├── test_tool_scheduler.py
├── test_app.py
├── test_openai_provider.py
├── test_anthropic_provider.py
└── test_tools.py
```

## 20. 测试策略

### 20.1 Agent 核心

- 无工具调用时一轮结束并产生最终回复。
- LLM → 工具 → 回填 → 下一轮的多轮 ReAct 流程。
- 用户消息、轮次、thinking、正文、工具和终止事件的准确顺序。
- thinking 不进入历史。
- 每次请求独立应用 `15` 轮上限。
- 规划和获批执行共用轮数。
- 第 15 轮不传工具并加入收尾提示。
- 第 15 轮仍收到工具调用时返回 `max_rounds_exceeded`。
- Provider 错误、LLM 超时、Token 上限、空响应和无效流。

### 20.2 Context 与审批

- 单工具 `allow_once` 只放行一个 `call_id`。
- 单工具 `reject` 产生 `tool_blocked_in_plan_mode`。
- 同批多个写和命令工具逐个审批。
- 最终计划执行、修改和拒绝。
- 当前请求临时授权不会影响下一请求。
- 未知、过期和重复审批标识被拒绝。
- 等待审批期间取消立即结束。

### 20.3 工具调度

- 连续读工具真实并发。
- 并发读结果按原始顺序返回。
- 写和命令工具严格串行。
- 混合批次使用读并发、写/命令屏障。
- 未知工具保持原始结果位置。
- 工具错误不会直接终止 Agent。
- 取消等待已启动工具并补齐未启动工具结果。

### 20.4 Provider

- OpenAI 与 Anthropic 正文流映射。
- 真实 thinking 分片映射；接口无 thinking 时不生成事件。
- 多工具参数分片组装。
- 停止原因归一化。
- `ProviderTurnEnd` 恰好出现一次且位于末尾。
- `system_prompt` 使用各协议的准确参数传递。

### 20.5 TUI

- TUI 只消费 Agent 事件，不直接运行工具循环。
- thinking、正文和工具状态增量显示。
- 工具审批卡片两个选项。
- 最终计划卡片三个选项。
- 第 15 轮计划卡片禁用执行和修改。
- 取消操作调用 `AgentRunContext.cancel()`。
- 请求终止后输入框恢复并获得焦点。

默认测试不得访问网络，也不得要求真实 API Key。

## 21. 验收标准

1. `AgentLoop` 和相关模块不导入 Textual。
2. `ChatApp` 中不再存在 ReAct 主循环。
3. 一条普通无工具请求只调用 LLM 一次并产生一个 `FinalResponseEvent`。
4. 工具调用结果准确回填并触发下一轮 LLM。
5. 连续读工具可以并发，写和命令工具不能并发。
6. 工具结果顺序与模型工具调用顺序一致。
7. plan-only 单工具授权只生效一次。
8. 最终计划授权只在当前用户请求内有效。
9. 下一条用户消息仍服从未关闭的 plan-only 开关。
10. 单个请求最多调用 LLM `15` 次。
11. 单轮 LLM 超过 `120` 秒时产生 `llm_timeout`。
12. 取消后没有后台工具继续无记录地修改系统。
13. 取消后的历史中不存在缺少结果的 `tool_call_id`。
14. thinking 只作为事件输出，不进入历史。
15. 两个 Provider 都输出统一结构化流事件。
16. 所有错误消息脱敏。
17. 默认测试不访问网络。
18. 以下命令全部成功：

```powershell
uv run python -m compileall -q src tests
uv run pytest
git diff --check
```

## 22. 参考实现取舍

已检查 `D:\实习\Mewcode\python` 中的实现。可参考的概念包括：

- `Agent.run() -> AsyncIterator[AgentEvent]`
- `StreamCollector`
- `ToolCategory`
- 连续安全工具分批
- 内联工具审批与计划审批组件

本章不直接复制以下设计：

- 单个超大 `agent.py` 同时承担事件、调度、权限和主循环。
- 在审批事件中向 UI 暴露 `asyncio.Future`。
- 在 LLM 流尚未结束时提前启动工具。
- 与本章范围无关的 Hooks 规则、记忆、上下文压缩、子 Agent 和团队能力。
