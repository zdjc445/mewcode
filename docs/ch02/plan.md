# ReAct Agent Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有 `ChatApp` 内的工具循环抽离为独立、可取消、可审批的 `AgentLoop`，并通过统一事件流支持双协议 Provider、工具调度和 plan-only。

**Architecture:** `AgentLoop` 是唯一的 ReAct 状态机，依赖 `LLMProvider`、`ConversationHistory`、`ToolScheduler` 和单次运行的 `AgentRunContext`，对外只返回 `AsyncIterator[AgentEvent]`。Provider 负责把 SDK 流归一化，调度器负责连续读并发和写/命令屏障，Textual 只消费事件并把卡片选择写回 Context。

**Tech Stack:** Python `>=3.11.9`、`asyncio`、`dataclasses`、OpenAI SDK `>=2.45.0`、Anthropic SDK `>=0.116.0`、Textual `>=8.2.8`、pytest `>=9.1.1`、pytest-asyncio `>=1.4.0`。

## Global Constraints

- 单次用户请求最多调用 LLM `15` 次；规划和获批执行共用计数器，不重新计数。
- 第 `15` 轮必须传 `tools=None`，并追加规格中的强制收尾提示。
- 单轮 LLM 超时为 `120.0` 秒；工具保留各自 `timeout_seconds`，默认 `30.0` 秒；审批和整个请求不设超时。
- `ToolCategory` 的合法值严格为 `Literal["read", "write", "command"]`。
- 连续 `read` 工具并发，`write`、`command` 和未知工具都是串行屏障；结果始终按模型原始调用顺序回填。
- plan-only 单工具授权只放行当前 `call_id`；最终计划授权只在当前用户请求内有效，且不关闭上层持有的 plan-only 开关。
- thinking 只从 Provider 的真实字段产生；无工具轮丢弃完整 thinking，工具调用轮把完整 thinking 作为 `thinking_blocks` 协议元数据保存并回传。
- `AgentLoop`、Context、Agent 事件和调度器不得导入 Textual。
- 默认测试不得访问网络，也不得要求真实 API Key。
- 本章只提供默认无操作工具拦截器，不实现完整权限策略、子 Agent、持久化或上下文压缩。

## File Responsibility Map

| 文件 | 本章职责 |
| --- | --- |
| `src/mewcode_agent/models.py` | `ThinkingBlock` 与带 thinking 元数据的 `ChatMessage` |
| `src/mewcode_agent/history.py` | 按协议顺序保存 assistant 工具调用、thinking 和工具结果 |
| `src/mewcode_agent/providers/base.py` | Provider 统一流事件、停止原因和 `LLMProvider` Protocol |
| `src/mewcode_agent/providers/openai_provider.py` | OpenAI 流、`reasoning_content`、工具参数和停止原因归一化 |
| `src/mewcode_agent/providers/anthropic_provider.py` | Anthropic thinking/signature、工具块和停止原因归一化 |
| `src/mewcode_agent/tools/base.py` | `ToolCategory` 与每个工具的 `category` 元数据 |
| `src/mewcode_agent/tools/registry.py` | 精确查找工具、生成协议 schema、保留现有执行错误和超时 |
| `src/mewcode_agent/agent/events.py` | 不可变 Agent 事件及审批选择类型 |
| `src/mewcode_agent/agent/context.py` | 单次运行、审批 Future 的内部管理和取消信号 |
| `src/mewcode_agent/agent/tool_scheduler.py` | 拦截器、审批、连续读并发、串行屏障、取消补齐结果 |
| `src/mewcode_agent/agent/loop.py` | Provider 轮次消费、ReAct 状态机、历史提交和终止事件 |
| `src/mewcode_agent/agent/__init__.py` | Agent 子包的稳定公开导出 |
| `src/mewcode_agent/app.py` | Textual 事件渲染、plan-only 开关和两类 Modal 卡片 |
| `src/mewcode_agent/cli.py` | 组装 Provider、Registry、History、AgentLoop 和 ChatApp |

## Spec Traceability

| `spec.md` 章节 | 实施 Task |
| --- | --- |
| 2–4 技术决策、术语、范围 | Global Constraints、Task 7、Task 8、Task 9 |
| 5 总体架构 | File Responsibility Map、Task 1–8 |
| 6 核心接口 | Task 2、Task 7 |
| 7 Provider 结构化流与 thinking | Task 4、Task 5、Task 6 |
| 8 Agent 对外事件 | Task 2 |
| 9 Agent 状态机 | Task 2、Task 3、Task 7 |
| 10 ReAct 循环 | Task 7 |
| 11 工具分类与调度 | Task 1、Task 3 |
| 12 plan-only 与审批 | Task 2、Task 3、Task 7、Task 8 |
| 13 工具执行拦截接口 | Task 3 |
| 14 历史一致性 | Task 4、Task 7 |
| 15 取消与超时 | Task 2、Task 3、Task 7、Task 8 |
| 16 错误模型 | Task 3、Task 7 |
| 17 最小系统提示词 | Task 7 |
| 18 TUI 集成 | Task 8 |
| 19 文件结构 | File Responsibility Map、Task 1–8 |
| 20 测试策略 | 每个 Task 的 red/green 步骤、Task 9 |
| 21 验收标准 | `checklist.md`、Task 9 |
| 22 参考实现取舍 | 保持模块化架构，不复制参考项目的单体 Agent、Future 事件或额外能力 |

---

### Task 1: 工具分类元数据

**Files:**

- Modify: `src/mewcode_agent/tools/base.py`
- Modify: `src/mewcode_agent/tools/read_file.py`
- Modify: `src/mewcode_agent/tools/find_files.py`
- Modify: `src/mewcode_agent/tools/search_code.py`
- Modify: `src/mewcode_agent/tools/write_file.py`
- Modify: `src/mewcode_agent/tools/edit_file.py`
- Modify: `src/mewcode_agent/tools/run_command.py`
- Modify: `src/mewcode_agent/tools/__init__.py`
- Modify: `tests/test_tools.py`

**Interfaces:**

- Produces: `ToolCategory = Literal["read", "write", "command"]`
- Produces: 每个 `Tool` 子类的准确 `category: ToolCategory`
- Preserves: `ToolRegistry.api_tools()` 的 OpenAI/Anthropic schema 不暴露 category

- [ ] **Step 1: 写六个内置工具分类的失败测试**

在 `tests/test_tools.py` 增加：

```python
@pytest.mark.parametrize(
    ("tool_name", "expected_category"),
    [
        ("read_file", "read"),
        ("find_files", "read"),
        ("search_code", "read"),
        ("write_file", "write"),
        ("edit_file", "write"),
        ("run_command", "command"),
    ],
)
def test_core_tools_have_exact_categories(
    tool_name: str,
    expected_category: str,
) -> None:
    tool = create_core_registry().get(tool_name)

    assert tool is not None
    assert tool.category == expected_category
```

- [ ] **Step 2: 运行分类测试，确认旧基类没有 category**

Run: `uv run pytest tests/test_tools.py::test_core_tools_have_exact_categories -v`

Expected: FAIL，错误包含 `has no attribute 'category'`。

- [ ] **Step 3: 在基类与六个工具上加入精确分类**

在 `tools/base.py` 增加：

```python
from typing import Any, Literal, TypeAlias

ToolCategory: TypeAlias = Literal["read", "write", "command"]


class Tool(ABC):
    name: str
    description: str
    parameters: dict[str, Any]
    category: ToolCategory
    timeout_seconds: float = 30.0
```

分别为工具类加入：

```python
class ReadFileTool(Tool):
    category = "read"

class FindFilesTool(Tool):
    category = "read"

class SearchCodeTool(Tool):
    category = "read"

class WriteFileTool(Tool):
    category = "write"

class EditFileTool(Tool):
    category = "write"

class RunCommandTool(Tool):
    category = "command"
```

从 `tools/__init__.py` 导出 `ToolCategory`。不得把 category 加进 Provider 工具 schema。

- [ ] **Step 4: 运行完整工具测试**

Run: `uv run pytest tests/test_tools.py -v`

Expected: PASS；现有文件状态、diff、参数校验与 `30.0` 秒默认超时测试不变。

- [ ] **Step 5: 提交 Task 4**

```powershell
git add -- src/mewcode_agent/tools tests/test_tools.py
git commit -m "Add tool execution categories"
```

---

### Task 2: Agent 事件与单次运行 Context

**Files:**

- Create: `src/mewcode_agent/agent/events.py`
- Create: `src/mewcode_agent/agent/context.py`
- Create: `src/mewcode_agent/agent/__init__.py`
- Create: `tests/test_agent_events.py`
- Create: `tests/test_agent_context.py`

**Interfaces:**

- Produces: 规格第 8 节列出的全部 `AgentEvent` dataclass 和第 9 节的 `AgentRunState`
- Produces: `ToolApprovalDecision`、`PlanApprovalDecision`、`PlanApprovalResolution`
- Produces: `AgentRunContext.begin_run()`、`finish_run()`、`cancel()` 和两个 public resolve 方法
- Produces for Agent internals: `open_*_approval()`、`wait_for_*_approval()`、`wait_cancelled()`、`cancelled`

- [ ] **Step 1: 写事件不可变性与 Context 生命周期失败测试**

在 `tests/test_agent_events.py` 写入：

```python
from dataclasses import FrozenInstanceError

import pytest

from mewcode_agent.agent.events import (
    FinalResponseEvent,
    PlanApprovalRequestedEvent,
    RoundStartedEvent,
    ToolApprovalRequestedEvent,
)


def test_agent_events_are_frozen_value_objects() -> None:
    event = RoundStartedEvent(1, 15, "planning")

    with pytest.raises(FrozenInstanceError):
        event.round_number = 2  # type: ignore[misc]


def test_approval_events_contain_ids_not_futures() -> None:
    tool_event = ToolApprovalRequestedEvent(
        "approval-1",
        "call-1",
        "write_file",
        "{}",
        "write",
    )
    plan_event = PlanApprovalRequestedEvent("approval-2", "计划", True, True)

    assert tool_event.request_id == "approval-1"
    assert plan_event.request_id == "approval-2"
    assert FinalResponseEvent("完成", 2).total_rounds == 2
```

在 `tests/test_agent_context.py` 写入：

```python
import asyncio

import pytest

from mewcode_agent.agent.context import AgentRunCancelled, AgentRunContext


@pytest.mark.asyncio
async def test_tool_approval_is_resolved_exactly_once() -> None:
    context = AgentRunContext()
    context.begin_run()
    request_id = context.open_tool_approval()
    waiter = asyncio.create_task(context.wait_for_tool_approval(request_id))

    context.resolve_tool_approval(request_id, "allow_once")

    assert await waiter == "allow_once"
    with pytest.raises(ValueError, match="未知、过期或已完成"):
        context.resolve_tool_approval(request_id, "reject")


@pytest.mark.asyncio
async def test_plan_changes_require_non_blank_feedback() -> None:
    context = AgentRunContext()
    context.begin_run()
    request_id = context.open_plan_approval()

    with pytest.raises(ValueError, match="feedback"):
        context.resolve_plan_approval(request_id, "request_changes", feedback=" ")

    context.resolve_plan_approval(
        request_id,
        "request_changes",
        feedback="补充回滚步骤",
    )
    resolution = await context.wait_for_plan_approval(request_id)
    assert resolution.decision == "request_changes"
    assert resolution.feedback == "补充回滚步骤"


@pytest.mark.asyncio
async def test_cancel_interrupts_approval_wait_and_is_idempotent() -> None:
    context = AgentRunContext()
    context.begin_run()
    request_id = context.open_tool_approval()
    waiter = asyncio.create_task(context.wait_for_tool_approval(request_id))

    context.cancel()
    context.cancel()

    with pytest.raises(AgentRunCancelled):
        await waiter


def test_context_can_begin_only_one_run() -> None:
    context = AgentRunContext()
    context.begin_run()
    context.finish_run()

    with pytest.raises(ValueError, match="只能服务一次"):
        context.begin_run()
```

再用参数化测试覆盖未知 request ID、非 `request_changes` 携带 feedback、等待期间取消和计划三种合法选择。

- [ ] **Step 2: 运行事件与 Context 测试，确认 Agent 包尚不存在**

Run: `uv run pytest tests/test_agent_events.py tests/test_agent_context.py -v`

Expected: FAIL，错误包含 `No module named 'mewcode_agent.agent'`。

- [ ] **Step 3: 创建不可变事件模型**

在 `agent/events.py` 精确定义规格中的字段和 union：

```python
from dataclasses import dataclass
from typing import Literal, TypeAlias

from mewcode_agent.tools.base import ToolCategory, ToolResult

AgentRunMode: TypeAlias = Literal["planning", "executing"]
AgentRunState: TypeAlias = Literal[
    "planning",
    "waiting_tool_approval",
    "waiting_plan_approval",
    "executing",
    "completed",
    "cancelled",
    "failed",
]
ToolApprovalDecision: TypeAlias = Literal["allow_once", "reject"]
PlanApprovalDecision: TypeAlias = Literal[
    "execute_current",
    "request_changes",
    "reject",
]


@dataclass(frozen=True, slots=True)
class UserMessageEvent:
    content: str


@dataclass(frozen=True, slots=True)
class RoundStartedEvent:
    round_number: int
    max_rounds: int
    mode: AgentRunMode


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


AgentEvent: TypeAlias = (
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

- [ ] **Step 4: 实现只使用一次的 `AgentRunContext`**

在 `agent/context.py` 使用 `uuid.uuid4().hex` 生成非空、不透明 ID，并定义：

```python
@dataclass(frozen=True, slots=True)
class PlanApprovalResolution:
    decision: PlanApprovalDecision
    feedback: str = ""


class AgentRunCancelled(Exception):
    """Internal control signal; never exposed as a UI error."""


class AgentRunContext:
    def __init__(self) -> None:
        self._used = False
        self._active = False
        self._cancelled = asyncio.Event()
        self._tool_approvals: dict[str, asyncio.Future[ToolApprovalDecision]] = {}
        self._plan_approvals: dict[str, asyncio.Future[PlanApprovalResolution]] = {}

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def begin_run(self) -> None:
        if self._used:
            raise ValueError("AgentRunContext 只能服务一次 run()")
        self._used = True
        self._active = True

    def finish_run(self) -> None:
        self._active = False
        for future in (*self._tool_approvals.values(), *self._plan_approvals.values()):
            future.cancel()
        self._tool_approvals.clear()
        self._plan_approvals.clear()

    def cancel(self) -> None:
        self._cancelled.set()

    async def wait_cancelled(self) -> None:
        await self._cancelled.wait()
```

`open_tool_approval()` 和 `open_plan_approval()` 必须检查 `_active`，创建当前 event loop 的 Future 并存入对应字典。两个 `resolve_*` 方法必须检查精确 decision、feedback 规则、未知 ID 和 `future.done()`；两个 `wait_for_*` 用 `asyncio.wait({future, cancel_task}, return_when=FIRST_COMPLETED)` 竞争审批与取消，取消优先抛 `AgentRunCancelled`，并在 `finally` 删除 ID、取消 `cancel_task`。

- [ ] **Step 5: 从 `agent/__init__.py` 导出稳定接口并运行测试**

`agent/__init__.py` 导出 `AgentEvent`、`AgentRunMode`、`AgentRunState`、全部事件、三类 decision/resolution 和 `AgentRunContext`；`AgentRunCancelled` 只供 Agent 子包内部导入。

Run: `uv run pytest tests/test_agent_events.py tests/test_agent_context.py -v`

Expected: PASS；没有 Future 出现在任何 Event 字段中。

- [ ] **Step 6: 提交 Task 5**

```powershell
git add -- src/mewcode_agent/agent tests/test_agent_events.py tests/test_agent_context.py
git commit -m "Add agent events and run context"
```

---

### Task 3: 工具调度、审批、拦截器与取消补齐

**Files:**

- Create: `src/mewcode_agent/agent/tool_scheduler.py`
- Modify: `src/mewcode_agent/agent/__init__.py`
- Create: `tests/test_tool_scheduler.py`

**Interfaces:**

- Consumes: `ToolRegistry`、`AgentRunContext`、`ToolCall`、Task 2 的审批与工具事件
- Produces: `ToolExecutionInterceptor`、`NoOpToolExecutionInterceptor`
- Produces: `ToolScheduler.run(tool_calls, *, plan_only, current_request_authorized, context) -> AsyncIterator[ToolSchedulerEvent]`
- Guarantees: 每个输入 `call_id` 恰好产生一个 `ToolResultEvent`

- [ ] **Step 1: 写连续读并发、屏障与顺序失败测试**

在 `tests/test_tool_scheduler.py` 定义以下测试工具和收集 helper：

```python
class ControlledTool(Tool):
    description = "controlled test tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name: str, category: ToolCategory) -> None:
        self.name = name
        self.category = category
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, arguments: dict[str, Any]) -> dict[str, str]:
        self.started.set()
        await self.release.wait()
        return {"name": self.name}


class TimelineTool(Tool):
    description = "timeline test tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(
        self,
        name: str,
        category: ToolCategory,
        timeline: list[str],
    ) -> None:
        self.name = name
        self.category = category
        self.timeline = timeline

    async def execute(self, arguments: dict[str, Any]) -> dict[str, str]:
        self.timeline.append(f"{self.name}:start")
        await asyncio.sleep(0)
        self.timeline.append(f"{self.name}:end")
        return {"name": self.name}


def make_timeline_registry(timeline: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    for name, category in (
        ("read_1", "read"),
        ("read_2", "read"),
        ("write_1", "write"),
        ("read_3", "read"),
        ("command_1", "command"),
    ):
        registry.register(TimelineTool(name, category, timeline))
    return registry


async def collect_scheduler_events(
    scheduler: ToolScheduler,
    calls: tuple[ToolCall, ...],
    *,
    context: AgentRunContext,
) -> list[ToolSchedulerEvent]:
    return [
        event
        async for event in scheduler.run(
            calls,
            plan_only=False,
            current_request_authorized=False,
            context=context,
        )
    ]
```

然后写：

```python
@pytest.mark.asyncio
async def test_consecutive_reads_run_concurrently_and_results_keep_call_order() -> None:
    first = ControlledTool("read_1", "read")
    second = ControlledTool("read_2", "read")
    registry = ToolRegistry()
    registry.register(first)
    registry.register(second)
    scheduler = ToolScheduler(registry)
    context = AgentRunContext()
    context.begin_run()

    task = asyncio.create_task(
        collect_scheduler_events(
            scheduler,
            (
                ToolCall("call_1", "read_1", "{}"),
                ToolCall("call_2", "read_2", "{}"),
            ),
            context=context,
        )
    )
    await first.started.wait()
    await second.started.wait()
    second.release.set()
    first.release.set()
    events = await task

    assert [event.call_id for event in events if isinstance(event, ToolCallStartedEvent)] == [
        "call_1",
        "call_2",
    ]
    assert [event.call_id for event in events if isinstance(event, ToolResultEvent)] == [
        "call_1",
        "call_2",
    ]


@pytest.mark.asyncio
async def test_write_and_command_are_serial_barriers() -> None:
    timeline: list[str] = []
    registry = make_timeline_registry(timeline)
    scheduler = ToolScheduler(registry)
    context = AgentRunContext()
    context.begin_run()

    events = [
        event
        async for event in scheduler.run(
            (
                ToolCall("1", "read_1", "{}"),
                ToolCall("2", "read_2", "{}"),
                ToolCall("3", "write_1", "{}"),
                ToolCall("4", "read_3", "{}"),
                ToolCall("5", "command_1", "{}"),
            ),
            plan_only=False,
            current_request_authorized=False,
            context=context,
        )
    ]

    assert timeline.index("read_1:end") < timeline.index("write_1:start")
    assert timeline.index("read_2:end") < timeline.index("write_1:start")
    assert timeline.index("write_1:end") < timeline.index("read_3:start")
    assert timeline.index("read_3:end") < timeline.index("command_1:start")
    assert [event.call_id for event in events if isinstance(event, ToolResultEvent)] == [
        "1", "2", "3", "4", "5"
    ]
```

- [ ] **Step 2: 写未知工具、plan-only 和取消失败测试**

覆盖以下准确断言：

```python
assert unknown_event.result.error_code == "tool_not_found"
assert rejected_event.result.error_code == "tool_blocked_in_plan_mode"
assert rejected_event.result.error_message == "工具在 plan-only 模式下被用户拒绝"
assert cancelled_event.result.error_code == "tool_cancelled"
assert cancelled_event.result.error_message == "工具因用户取消而未执行"
```

测试流程必须包括：read 自动执行；write/command 各产生一个 `ToolApprovalRequestedEvent`；`allow_once` 只放行当前调用；`reject` 不产生对应 `ToolCallStartedEvent`；`current_request_authorized=True` 时 write/command 不审批；并发读组取消后等待已启动 read 完成，后续调用全部补 `tool_cancelled`。

另写拦截器测试，确认 `before_execute()` 可以返回 ToolResult 阻止 registry 执行，`after_execute()` 可以转换最终结果，默认实现原样通过。

- [ ] **Step 3: 运行调度器测试，确认模块尚不存在**

Run: `uv run pytest tests/test_tool_scheduler.py -v`

Expected: FAIL，错误包含 `No module named 'mewcode_agent.agent.tool_scheduler'`。

- [ ] **Step 4: 定义拦截器和调度器事件类型**

在 `agent/tool_scheduler.py` 定义：

```python
ToolSchedulerEvent: TypeAlias = (
    ToolApprovalRequestedEvent | ToolCallStartedEvent | ToolResultEvent
)


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


class NoOpToolExecutionInterceptor:
    async def before_execute(
        self,
        tool_call: ToolCall,
        *,
        plan_only: bool,
        current_request_authorized: bool,
    ) -> ToolResult | None:
        return None

    async def after_execute(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        return result
```

`ToolScheduler.__init__()` 精确为：

```python
def __init__(
    self,
    registry: ToolRegistry,
    *,
    interceptor: ToolExecutionInterceptor | None = None,
) -> None:
    self._registry = registry
    self._interceptor = interceptor or NoOpToolExecutionInterceptor()
```

- [ ] **Step 5: 实现分组、审批和执行算法**

`run()` 签名：

```python
async def run(
    self,
    tool_calls: tuple[ToolCall, ...],
    *,
    plan_only: bool,
    current_request_authorized: bool,
    context: AgentRunContext,
) -> AsyncIterator[ToolSchedulerEvent]:
```

算法必须按索引扫描：只把相邻且已注册、`category == "read"` 的调用组成一个 gather 批次；write、command 和未知工具每个单独成组。未知工具直接产生当前位置的 `tool_not_found`，不得发 started 事件。

每个已获准调用先发：

```python
ToolCallStartedEvent(
    call_id=call.call_id,
    tool_name=call.name,
    arguments_json=call.arguments_json,
    category=tool.category,
)
```

连续读批次在全部 started 事件发出后执行：

```python
results = await asyncio.gather(
    *(
        self._execute_one(
            call,
            plan_only=plan_only,
            current_request_authorized=current_request_authorized,
        )
        for call in read_group
    )
)
for call, result in zip(read_group, results, strict=True):
    yield ToolResultEvent(call.call_id, result)
```

plan-only 且未获得当前请求授权时，write/command 依次执行：`open_tool_approval()` → yield `ToolApprovalRequestedEvent` → await `wait_for_tool_approval()`。拒绝时直接产生规格中的 `tool_blocked_in_plan_mode`。

一旦 `context.cancelled` 或等待审批抛 `AgentRunCancelled`，不得启动后续调用；当前及后续未启动调用按原顺序产生规格中的 `tool_cancelled`。已经发出 started 的单工具或 read 组等待真实结果后，再补齐未启动结果。

- [ ] **Step 6: 运行调度与工具回归测试**

Run: `uv run pytest tests/test_tool_scheduler.py tests/test_tools.py -v`

Expected: PASS；每个输入 `call_id` 恰好对应一个 `ToolResultEvent`，工具 registry 的原错误映射保持通过。

- [ ] **Step 7: 提交 Task 6**

```powershell
git add -- src/mewcode_agent/agent/tool_scheduler.py src/mewcode_agent/agent/__init__.py tests/test_tool_scheduler.py
git commit -m "Add ordered tool scheduler"
```

---

### Task 4: thinking 数据模型与历史约束

**Files:**

- Modify: `src/mewcode_agent/models.py`
- Modify: `src/mewcode_agent/history.py`
- Create: `tests/test_models.py`
- Modify: `tests/test_history.py`

**Interfaces:**

- Produces: `ThinkingBlock(text: str, signature: str = "")`
- Produces: `ChatMessage.thinking_blocks: tuple[ThinkingBlock, ...]`
- Produces: `ConversationHistory.add_assistant_tool_call(..., *, thinking_blocks=())`
- Produces: `ConversationHistory.add_assistant_tool_calls(..., *, thinking_blocks=())`

- [ ] **Step 1: 为合法与非法 thinking 组合写失败测试**

在 `tests/test_models.py` 写入：

```python
import pytest

from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall


def test_assistant_tool_call_accepts_thinking_blocks() -> None:
    call = ToolCall("call_1", "read_file", '{"path":"README.md"}')
    block = ThinkingBlock("先读取文件", "sig-1")

    message = ChatMessage(
        role="assistant",
        content="",
        tool_calls=(call,),
        thinking_blocks=(block,),
    )

    assert message.thinking_blocks == (block,)


@pytest.mark.parametrize(
    "message",
    [
        ChatMessage(role="user", content="问题"),
        ChatMessage(role="assistant", content="答案"),
        ChatMessage(role="tool", content="结果", tool_call_id="call_1"),
    ],
)
def test_non_tool_call_messages_reject_thinking_blocks(message: ChatMessage) -> None:
    with pytest.raises(ValueError, match="thinking_blocks"):
        ChatMessage(
            role=message.role,
            content=message.content,
            tool_calls=message.tool_calls,
            tool_call_id=message.tool_call_id,
            thinking_blocks=(ThinkingBlock("不能保存"),),
        )


@pytest.mark.parametrize("text", ["", " ", "\n\t"])
def test_thinking_block_rejects_blank_text(text: str) -> None:
    with pytest.raises(ValueError, match="text 必须"):
        ThinkingBlock(text)


def test_thinking_block_rejects_non_string_signature() -> None:
    with pytest.raises(ValueError, match="signature 必须"):
        ThinkingBlock("内容", signature=1)  # type: ignore[arg-type]
```

在 `tests/test_history.py` 增加：

```python
from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall


def test_history_keeps_thinking_on_assistant_tool_call() -> None:
    history = ConversationHistory()
    call = ToolCall("call_1", "read_file", '{"path":"README.md"}')
    block = ThinkingBlock("读取后回答", "sig-1")

    history.add_assistant_tool_calls(
        "",
        (call,),
        thinking_blocks=(block,),
    )

    assert history.snapshot() == [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=(call,),
            thinking_blocks=(block,),
        )
    ]
```

- [ ] **Step 2: 运行模型与历史测试，确认新接口尚不存在**

Run: `uv run pytest tests/test_models.py tests/test_history.py -v`

Expected: FAIL，错误包含 `cannot import name 'ThinkingBlock'` 或 `unexpected keyword argument 'thinking_blocks'`。

- [ ] **Step 3: 实现 `ThinkingBlock`、`ChatMessage` 约束和历史入口**

在 `src/mewcode_agent/models.py` 的 `ToolCall` 后加入：

```python
@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    """One complete provider reasoning block required by tool-call history."""

    text: str
    signature: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text 必须为非空字符串")
        if not isinstance(self.signature, str):
            raise ValueError("signature 必须为字符串")
```

把 `ChatMessage` 字段与 `__post_init__()` 的工具调用分支改为：

```python
    role: ChatRole
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    thinking_blocks: tuple[ThinkingBlock, ...] = ()

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant", "tool"):
            raise ValueError("role 必须为 user、assistant 或 tool")
        if not isinstance(self.content, str):
            raise ValueError("content 必须为字符串")
        if self.role == "assistant" and self.tool_calls:
            if self.tool_call_id is not None:
                raise ValueError("assistant 消息不能包含 tool_call_id")
            return
        if self.thinking_blocks:
            raise ValueError("只有 assistant 工具调用消息可以包含 thinking_blocks")
        if not self.content.strip():
            raise ValueError("content 必须为非空字符串")
        if self.role == "tool":
            if not self.tool_call_id:
                raise ValueError("tool 消息必须包含 tool_call_id")
            if self.tool_calls:
                raise ValueError("tool 消息不能包含 tool_calls")
        elif self.tool_calls or self.tool_call_id is not None:
            raise ValueError(f"{self.role} 消息不能包含工具字段")
```

在 `src/mewcode_agent/history.py` 导入 `ThinkingBlock`，并把两个方法改成：

```python
    def add_assistant_tool_call(
        self,
        content: str,
        tool_call: ToolCall,
        *,
        thinking_blocks: tuple[ThinkingBlock, ...] = (),
    ) -> ChatMessage:
        return self.add_assistant_tool_calls(
            content,
            (tool_call,),
            thinking_blocks=thinking_blocks,
        )

    def add_assistant_tool_calls(
        self,
        content: str,
        tool_calls: tuple[ToolCall, ...],
        *,
        thinking_blocks: tuple[ThinkingBlock, ...] = (),
    ) -> ChatMessage:
        if not tool_calls:
            raise ValueError("tool_calls 不能为空")
        message = ChatMessage(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            thinking_blocks=thinking_blocks,
        )
        self._messages.append(message)
        return message
```

- [ ] **Step 4: 运行 Task 4 测试**

Run: `uv run pytest tests/test_models.py tests/test_history.py -v`

Expected: PASS，且现有历史顺序与空白校验测试继续通过。

- [ ] **Step 5: 提交 Task 4**

```powershell
git add -- src/mewcode_agent/models.py src/mewcode_agent/history.py tests/test_models.py tests/test_history.py
git commit -m "Add thinking history metadata"
```

---

### Task 5: Provider 统一事件契约与 OpenAI 适配

**Files:**

- Modify: `src/mewcode_agent/providers/base.py`
- Modify: `src/mewcode_agent/providers/__init__.py`
- Modify: `src/mewcode_agent/providers/openai_provider.py`
- Modify: `tests/test_openai_provider.py`

**Interfaces:**

- Consumes: `ThinkingBlock`、`ChatMessage.thinking_blocks`
- Produces: `ProviderThinkingDelta`、`ProviderThinkingComplete`、`ProviderTextDelta`、`ProviderToolCall`、`ProviderTurnEnd`
- Produces: `ProviderStopReason = Literal["end_turn", "tool_calls", "max_tokens", "other"]`
- Changes: `LLMProvider.stream_chat(..., tools=None, system_prompt: str) -> AsyncIterator[ProviderStreamEvent]`

- [ ] **Step 1: 把 OpenAI 测试改成统一事件、system prompt 和 reasoning 回传**

保留现有 SDK 错误映射测试；把 fake chunk 的选择对象增加 `finish_reason`，并新增以下断言：

```python
from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall
from mewcode_agent.providers.base import (
    ProviderTextDelta,
    ProviderThinkingComplete,
    ProviderThinkingDelta,
    ProviderToolCall,
    ProviderTurnEnd,
)


def make_chunk(
    text: str | None,
    *,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
    with_choices: bool = True,
) -> Any:
    choices = []
    if with_choices:
        delta = SimpleNamespace(
            content=text,
            reasoning_content=reasoning_content,
            tool_calls=None,
        )
        choices.append(SimpleNamespace(delta=delta, finish_reason=finish_reason))
    return SimpleNamespace(choices=choices)


@pytest.mark.asyncio
async def test_openai_provider_maps_thinking_text_and_stop_reason(
    openai_config: ProviderConfig,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream(
            [
                make_chunk(None, reasoning_content="先分析"),
                make_chunk("答案", finish_reason="stop"),
            ]
        )
    )
    provider = OpenAIProvider(openai_config, "test-secret", client=make_client(create))

    events = [
        event
        async for event in provider.stream_chat(
            [ChatMessage(role="user", content="问题")],
            system_prompt="system text",
        )
    ]

    assert events == [
        ProviderThinkingDelta("先分析"),
        ProviderTextDelta("答案"),
        ProviderThinkingComplete(ThinkingBlock("先分析")),
        ProviderTurnEnd("end_turn"),
    ]
    assert create.kwargs["messages"][0] == {
        "role": "system",
        "content": "system text",
    }


def test_openai_provider_serializes_reasoning_for_tool_history() -> None:
    call = ToolCall("call_1", "read_file", '{"path":"README.md"}')
    request = OpenAIProvider._request_messages(
        [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=(call,),
                thinking_blocks=(ThinkingBlock("先分析"), ThinkingBlock("再调用")),
            )
        ],
        system_prompt="system text",
    )

    assert request[1]["reasoning_content"] == "先分析再调用"
    assert request[1]["content"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_reason", "expected"),
    [
        ("stop", "end_turn"),
        ("tool_calls", "tool_calls"),
        ("length", "max_tokens"),
        ("content_filter", "other"),
        (None, "other"),
    ],
)
async def test_openai_provider_maps_finish_reason(
    openai_config: ProviderConfig,
    raw_reason: str | None,
    expected: str,
) -> None:
    create = FakeOpenAICreate(
        FakeOpenAIStream([make_chunk("x", finish_reason=raw_reason)])
    )
    provider = OpenAIProvider(openai_config, "test-secret", client=make_client(create))

    events = [
        event
        async for event in provider.stream_chat(
            [ChatMessage(role="user", content="问题")],
            system_prompt="system text",
        )
    ]

    assert events[-1] == ProviderTurnEnd(expected)  # type: ignore[arg-type]
```

工具参数测试改为断言 `ProviderToolCall(ToolCall(...))`，所有 `stream_chat()` 调用都显式传 `system_prompt="system text"`。

- [ ] **Step 2: 运行 OpenAI Provider 测试，确认旧字符串协议失败**

Run: `uv run pytest tests/test_openai_provider.py -v`

Expected: FAIL，错误包含无法导入 `ProviderTextDelta` 或 `stream_chat()` 不接受 `system_prompt`。

- [ ] **Step 3: 定义 Provider 统一事件和 Protocol**

用以下内容替换 `StreamPart`，并修改 Protocol：

```python
from dataclasses import dataclass

from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall

ProviderProtocol: TypeAlias = Literal["openai", "anthropic"]
ProviderStopReason: TypeAlias = Literal[
    "end_turn",
    "tool_calls",
    "max_tokens",
    "other",
]


@dataclass(frozen=True, slots=True)
class ProviderThinkingDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ProviderThinkingComplete:
    block: ThinkingBlock


@dataclass(frozen=True, slots=True)
class ProviderTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    tool_call: ToolCall


@dataclass(frozen=True, slots=True)
class ProviderTurnEnd:
    stop_reason: ProviderStopReason


ProviderStreamEvent: TypeAlias = (
    ProviderThinkingDelta
    | ProviderThinkingComplete
    | ProviderTextDelta
    | ProviderToolCall
    | ProviderTurnEnd
)


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

在 `providers/__init__.py` 精确导出这些公开类型。

- [ ] **Step 4: 把 OpenAI 流转换成统一事件**

实现时使用以下确定映射：

```python
OPENAI_STOP_REASON_MAP: dict[str, ProviderStopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_calls",
    "length": "max_tokens",
}
```

`_request_messages()` 的入口改为：

```python
@staticmethod
def _request_messages(
    messages: list[ChatMessage],
    *,
    system_prompt: str,
) -> list[dict[str, Any]]:
    request_messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]
```

assistant 工具调用字典在存在 `thinking_blocks` 时加入：

```python
payload["reasoning_content"] = "".join(
    block.text for block in message.thinking_blocks
)
```

流循环严格按以下顺序收集并输出：

```python
reasoning_parts: list[str] = []
finish_reason: str | None = None

reasoning = getattr(delta, "reasoning_content", None)
if reasoning:
    reasoning_parts.append(reasoning)
    yield ProviderThinkingDelta(reasoning)
if text:
    yield ProviderTextDelta(text)
finish_reason = choice.finish_reason or finish_reason

# SDK 流结束后：thinking complete → 按 index 排序的完整工具调用 → turn end
if reasoning_parts:
    yield ProviderThinkingComplete(ThinkingBlock("".join(reasoning_parts)))
for tool_call in tool_calls:
    yield ProviderToolCall(tool_call)
yield ProviderTurnEnd(OPENAI_STOP_REASON_MAP.get(finish_reason or "", "other"))
```

Provider 不再自行拒绝空正文；空响应与停止原因一致性统一由 `AgentLoop` 判定。现有 SDK 异常仍按原脱敏文案转换为 `ProviderError`。

- [ ] **Step 5: 运行 Provider 基础与 OpenAI 测试**

Run: `uv run pytest tests/test_openai_provider.py tests/test_provider_factory.py -v`

Expected: PASS；OpenAI 每个测试流的最后一个对象都是 `ProviderTurnEnd`。

- [ ] **Step 6: 提交 Task 5**

```powershell
git add -- src/mewcode_agent/providers/base.py src/mewcode_agent/providers/__init__.py src/mewcode_agent/providers/openai_provider.py tests/test_openai_provider.py
git commit -m "Add structured OpenAI provider events"
```

---

### Task 6: Anthropic thinking、signature 与停止原因适配

**Files:**

- Modify: `src/mewcode_agent/providers/anthropic_provider.py`
- Modify: `tests/test_anthropic_provider.py`

**Interfaces:**

- Consumes: Task 5 的 `ProviderStreamEvent` 系列类型
- Produces: Anthropic `thinking_delta`、`signature_delta`、`tool_use` 和 `message_delta.stop_reason` 的统一映射
- Preserves: `ThinkingBlock.signature` 在工具调用历史中的原值

- [ ] **Step 1: 写 Anthropic 结构化流与 thinking 回传失败测试**

把测试 fake 统一成事件流 manager，不再使用 `text_stream` 特例，并新增：

```python
@pytest.mark.asyncio
async def test_anthropic_provider_maps_thinking_signature_and_stop_reason(
    anthropic_config: ProviderConfig,
) -> None:
    events = [
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="thinking", thinking="", signature=""),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking="先分析"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="signature_delta", signature="sig-1"),
        ),
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="text_delta", text="答案"),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
        ),
    ]
    stream = FakeAnthropicStream(FakeAnthropicEventManager(events))
    provider = AnthropicProvider(
        anthropic_config,
        "test-secret",
        client=make_client(stream),
    )

    result = [
        event
        async for event in provider.stream_chat(
            [ChatMessage(role="user", content="问题")],
            system_prompt="system text",
        )
    ]

    assert result == [
        ProviderThinkingDelta("先分析"),
        ProviderThinkingComplete(ThinkingBlock("先分析", "sig-1")),
        ProviderTextDelta("答案"),
        ProviderTurnEnd("end_turn"),
    ]
    assert stream.kwargs["system"] == "system text"


def test_anthropic_provider_serializes_thinking_before_tool_use() -> None:
    call = ToolCall("toolu_1", "read_file", '{"path":"README.md"}')
    request = AnthropicProvider._request_messages(
        [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=(call,),
                thinking_blocks=(ThinkingBlock("先分析", "sig-1"),),
            )
        ]
    )

    assert request[0]["content"][0] == {
        "type": "thinking",
        "thinking": "先分析",
        "signature": "sig-1",
    }
    assert request[0]["content"][1]["type"] == "tool_use"
```

增加停止原因参数化测试：`end_turn → end_turn`、`tool_use → tool_calls`、`max_tokens → max_tokens`、其他或缺失 → `other`。

- [ ] **Step 2: 运行 Anthropic 测试，确认旧 text/tool union 失败**

Run: `uv run pytest tests/test_anthropic_provider.py -v`

Expected: FAIL，旧实现没有 `system_prompt`、thinking 完整块和 `ProviderTurnEnd`。

- [ ] **Step 3: 实现 Anthropic 全事件流解析**

使用以下停止原因映射：

```python
ANTHROPIC_STOP_REASON_MAP: dict[str, ProviderStopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_calls",
    "max_tokens": "max_tokens",
}
```

所有请求都通过 `async with self._client.messages.stream(**request)` 的事件迭代器消费，并设置：

```python
request: dict[str, Any] = {
    "model": self._config.model,
    "messages": self._request_messages(messages),
    "max_tokens": self._config.max_tokens,
    "system": system_prompt,
}
```

按 content block index 保存：

```python
thinking_blocks: dict[int, dict[str, str]] = {}
streamed_tool_calls: dict[int, dict[str, str]] = {}
stop_reason: str | None = None
```

事件处理必须执行以下准确动作：

```python
if event.type == "content_block_start" and event.content_block.type == "thinking":
    thinking_blocks[event.index] = {
        "text": getattr(event.content_block, "thinking", "") or "",
        "signature": getattr(event.content_block, "signature", "") or "",
    }
elif event.type == "content_block_delta" and event.delta.type == "thinking_delta":
    thinking_blocks[event.index]["text"] += event.delta.thinking
    yield ProviderThinkingDelta(event.delta.thinking)
elif event.type == "content_block_delta" and event.delta.type == "signature_delta":
    thinking_blocks[event.index]["signature"] += event.delta.signature
elif event.type == "content_block_stop" and event.index in thinking_blocks:
    raw = thinking_blocks.pop(event.index)
    yield ProviderThinkingComplete(ThinkingBlock(raw["text"], raw["signature"]))
elif event.type == "content_block_delta" and event.delta.type == "text_delta":
    yield ProviderTextDelta(event.delta.text)
elif event.type == "message_delta":
    stop_reason = event.delta.stop_reason or stop_reason
```

流结束后先按 index 输出 `ProviderToolCall`，最后输出：

```python
yield ProviderTurnEnd(
    ANTHROPIC_STOP_REASON_MAP.get(stop_reason or "", "other")
)
```

`_request_messages()` 在 assistant 的文本和 `tool_use` 之前，按 `message.thinking_blocks` 顺序加入带原始 `signature` 的 thinking block。保留现有工具结果合并规则和 SDK 脱敏错误映射。

- [ ] **Step 4: 运行两个 Provider 测试，防止协议分叉回归**

Run: `uv run pytest tests/test_openai_provider.py tests/test_anthropic_provider.py -v`

Expected: PASS；两套 Provider 都只输出统一事件，并且每轮最后恰好一个 `ProviderTurnEnd`。

- [ ] **Step 5: 提交 Task 6**

```powershell
git add -- src/mewcode_agent/providers/anthropic_provider.py tests/test_anthropic_provider.py
git commit -m "Add structured Anthropic provider events"
```

---

### Task 7: 独立 ReAct `AgentLoop`

**Files:**

- Create: `src/mewcode_agent/agent/loop.py`
- Modify: `src/mewcode_agent/agent/__init__.py`
- Create: `tests/test_agent_loop.py`

**Interfaces:**

- Consumes: Task 2 Context、Task 3 Scheduler、Task 5/6 Provider 事件、`ConversationHistory`
- Produces: `AgentLoopConfig(max_rounds: int = 15, llm_timeout_seconds: float = 120.0)`
- Produces: `AgentLoop.run(user_message, history, *, plan_only, context) -> AsyncIterator[AgentEvent]`
- Guarantees: 一个 run 只产生 `FinalResponseEvent`、`RunErrorEvent`、`RunCancelledEvent` 三者之一，且终止事件后不再输出事件

- [ ] **Step 1: 建立可记录每轮输入的结构化 Fake Provider**

在 `tests/test_agent_loop.py` 写入公共测试夹具：

```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from mewcode_agent.agent import (
    AgentLoop,
    AgentLoopConfig,
    AgentRunContext,
    FinalResponseEvent,
    ModelTextEvent,
    ModelThinkingEvent,
    PlanApprovalRequestedEvent,
    RoundStartedEvent,
    RunCancelledEvent,
    RunErrorEvent,
    ToolCallStartedEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall
from mewcode_agent.providers.base import (
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderThinkingComplete,
    ProviderThinkingDelta,
    ProviderToolCall,
    ProviderTurnEnd,
)
from mewcode_agent.tools import Tool, ToolRegistry


class ScriptedProvider:
    protocol = "openai"

    def __init__(self, rounds: list[list[ProviderStreamEvent]]) -> None:
        self.rounds = rounds
        self.requests: list[list[ChatMessage]] = []
        self.tools: list[list[dict[str, Any]] | None] = []
        self.system_prompts: list[str] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str,
    ) -> AsyncIterator[ProviderStreamEvent]:
        index = len(self.requests)
        self.requests.append(messages)
        self.tools.append(tools)
        self.system_prompts.append(system_prompt)
        for event in self.rounds[index]:
            yield event


class EchoTool(Tool):
    name = "echo_read"
    description = "Return the input value"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
    }
    category = "read"

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"value": arguments["value"]}


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


async def collect_run(
    loop: AgentLoop,
    message: str,
    history: ConversationHistory,
    *,
    plan_only: bool = False,
    context: AgentRunContext | None = None,
) -> list[object]:
    run_context = context or AgentRunContext()
    return [
        event
        async for event in loop.run(
            message,
            history,
            plan_only=plan_only,
            context=run_context,
        )
    ]
```

- [ ] **Step 2: 写普通结束、ReAct 和 thinking 历史失败测试**

加入以下核心测试：

```python
@pytest.mark.asyncio
async def test_one_round_text_response_commits_history_before_final_event() -> None:
    provider = ScriptedProvider(
        [[ProviderThinkingDelta("分析"), ProviderThinkingComplete(ThinkingBlock("分析")), ProviderTextDelta("完成"), ProviderTurnEnd("end_turn")]]
    )
    history = ConversationHistory()
    loop = AgentLoop(provider, make_registry())

    events = await collect_run(loop, "任务", history)

    assert events == [
        UserMessageEvent("任务"),
        RoundStartedEvent(1, 15, "executing"),
        ModelThinkingEvent("分析"),
        ModelTextEvent("完成"),
        FinalResponseEvent("完成", 1),
    ]
    assert history.snapshot() == [
        ChatMessage(role="user", content="任务"),
        ChatMessage(role="assistant", content="完成"),
    ]


@pytest.mark.asyncio
async def test_tool_round_commits_thinking_results_then_calls_model_again() -> None:
    call = ToolCall("call_1", "echo_read", '{"value":7}')
    provider = ScriptedProvider(
        [
            [
                ProviderThinkingDelta("需要读取"),
                ProviderThinkingComplete(ThinkingBlock("需要读取")),
                ProviderToolCall(call),
                ProviderTurnEnd("tool_calls"),
            ],
            [ProviderTextDelta("值是 7"), ProviderTurnEnd("end_turn")],
        ]
    )
    history = ConversationHistory()
    loop = AgentLoop(provider, make_registry())

    events = await collect_run(loop, "读取值", history)

    assert [type(event) for event in events] == [
        UserMessageEvent,
        RoundStartedEvent,
        ModelThinkingEvent,
        ToolCallStartedEvent,
        ToolResultEvent,
        RoundStartedEvent,
        ModelTextEvent,
        FinalResponseEvent,
    ]
    tool_message = history.snapshot()[1]
    assert tool_message.tool_calls == (call,)
    assert tool_message.content == ""
    assert tool_message.thinking_blocks == (ThinkingBlock("需要读取"),)
    assert provider.requests[1] == history.snapshot()[:3]
```

第一个测试明确证明无工具轮的完整 thinking 被丢弃；第二个测试明确证明工具轮 thinking 只进 `thinking_blocks`。

- [ ] **Step 3: 写 Provider 流校验、超时、取消和 15 轮失败测试**

用参数化脚本覆盖：

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "expected_code"),
    [
        ([], "invalid_provider_stream"),
        ([ProviderTurnEnd("end_turn")], "empty_response"),
        ([ProviderThinkingDelta("只有分析"), ProviderThinkingComplete(ThinkingBlock("只有分析")), ProviderTurnEnd("end_turn")], "invalid_provider_stream"),
        ([ProviderToolCall(ToolCall("1", "echo_read", "{}")), ProviderTurnEnd("end_turn")], "invalid_provider_stream"),
        ([ProviderTextDelta("正文"), ProviderTurnEnd("tool_calls")], "invalid_provider_stream"),
        ([ProviderTextDelta("未完成"), ProviderTurnEnd("max_tokens")], "max_tokens_reached"),
        ([ProviderTurnEnd("end_turn"), ProviderTextDelta("结尾后事件")], "invalid_provider_stream"),
    ],
)
async def test_invalid_provider_streams_return_one_terminal_error(
    events: list[ProviderStreamEvent],
    expected_code: str,
) -> None:
    history = ConversationHistory()
    result = await collect_run(
        AgentLoop(ScriptedProvider([events]), make_registry()),
        "任务",
        history,
    )

    terminal = [event for event in result if isinstance(event, RunErrorEvent)]
    assert len(terminal) == 1
    assert terminal[0].code == expected_code
    assert history.snapshot() == [ChatMessage(role="user", content="任务")]
```

新增 `SlowProvider`，用 `AgentLoopConfig(llm_timeout_seconds=0.01)` 断言 `llm_timeout`；新增在首个 `ProviderTextDelta` 后等待 Event 的 Provider，消费到 `ModelTextEvent` 后调用 `context.cancel()`，断言只保留 user 历史并以 `RunCancelledEvent("user_cancelled")` 终止。

新增抛 `ProviderError("已脱敏错误")` 的 Provider，断言唯一终止事件为 `RunErrorEvent("provider_error", "已脱敏错误")`。参数化测试 `AgentLoopConfig(max_rounds=0)`、`AgentLoopConfig(llm_timeout_seconds=0)` 分别抛出对应字段的 `ValueError`；空白 `user_message` 在写历史和 `context.begin_run()` 前抛 `ValueError`。

15 轮测试使用前 14 轮 read 工具调用和第 15 轮正文，精确断言：

```python
assert len(provider.requests) == 15
assert all(tools is not None for tools in provider.tools[:14])
assert provider.tools[14] is None
assert "This is the final allowed model round. Do not request tools." in provider.system_prompts[14]
```

另一个第 15 轮脚本返回 `ProviderToolCall`，断言 `max_rounds_exceeded` 且第 15 个工具不执行。

再用真实 `ToolScheduler` 增加工具批次取消集成测试：assistant 工具调用进入历史后取消，等待已启动调用完成；断言 history 中每个原始 `call_id` 恰好有一个 tool 消息，未启动项的 JSON 结果包含 `"code":"tool_cancelled"`，最后只产生 `RunCancelledEvent`，且 Provider 不进入下一轮。

- [ ] **Step 4: 写 plan-only 三种计划选择和临时授权失败测试**

使用边消费边解决卡片的 helper：

```python
async def collect_with_plan_decisions(
    loop: AgentLoop,
    history: ConversationHistory,
    context: AgentRunContext,
    decisions: list[tuple[str, str]],
) -> list[object]:
    events: list[object] = []
    async for event in loop.run(
        "规划任务",
        history,
        plan_only=True,
        context=context,
    ):
        events.append(event)
        if isinstance(event, PlanApprovalRequestedEvent):
            decision, feedback = decisions.pop(0)
            context.resolve_plan_approval(
                event.request_id,
                decision,  # type: ignore[arg-type]
                feedback=feedback,
            )
    return events
```

覆盖准确行为：

- `execute_current`：历史追加 `ChatMessage(role="user", content="计划已批准，请执行当前计划。")`，不产生对应 `UserMessageEvent`，后续 mode 为 `executing`，后续 write/command 不再逐次审批。
- `request_changes`：反馈作为 user 历史并产生 `UserMessageEvent(feedback)`，后续 mode 仍是 `planning`。
- `reject`：以 `RunCancelledEvent("plan_rejected")` 终止。
- 第 15 轮计划：`can_execute=False`、`can_request_changes=False`，用户 reject 后以 `RunCancelledEvent("round_limit_after_plan")` 终止。
- 新建第二个 Context 发起下一次 `plan_only=True` 请求，证明上一请求的临时授权没有存进 `AgentLoop`。

同时断言 planning 首轮 system prompt 与 `PLANNING_PROMPT` 完全相等；批准后下一轮 system prompt 等于 `EXECUTION_PROMPT + "\n" + APPROVED_PLAN_PROMPT`，没有修改或重排规格文本。

- [ ] **Step 5: 运行 AgentLoop 测试，确认模块尚不存在**

Run: `uv run pytest tests/test_agent_loop.py -v`

Expected: FAIL，错误包含无法导入 `AgentLoop` 或 `AgentLoopConfig`。

- [ ] **Step 6: 实现配置、提示词和构造函数**

在 `agent/loop.py` 定义准确常量：

```python
EXECUTION_PROMPT = """\
You are a coding agent. Use the available tools when needed.
When the task is complete, return a final response without tool calls."""

PLANNING_PROMPT = """\
You are in plan-only mode. Inspect the project with read tools and produce
an implementation plan. Write and command tools require user approval."""

APPROVED_PLAN_PROMPT = """\
The user approved the current plan. Execute it for this request.
The approval expires when this request ends."""

FINAL_ROUND_PROMPT = """\
This is the final allowed model round. Do not request tools.
Return the best final response using the available results."""


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    max_rounds: int = 15
    llm_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.max_rounds <= 0:
            raise ValueError("max_rounds 必须大于 0")
        if self.llm_timeout_seconds <= 0:
            raise ValueError("llm_timeout_seconds 必须大于 0")


class AgentLoop:
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        *,
        config: AgentLoopConfig | None = None,
        scheduler: ToolScheduler | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._config = config or AgentLoopConfig()
        self._scheduler = scheduler or ToolScheduler(registry)
```

提示词组合规则固定为：planning 使用 `PLANNING_PROMPT`；普通 executing 使用 `EXECUTION_PROMPT`；最终计划获批后的 executing 在执行提示后追加 `APPROVED_PLAN_PROMPT`；最后一轮在当前提示后追加 `FINAL_ROUND_PROMPT`，片段之间用 `"\n"` 连接。

- [ ] **Step 7: 实现可取消、带总轮超时的 Provider 消费器**

创建无界 `asyncio.Queue` 和 provider producer task。producer 在 `asyncio.timeout(self._config.llm_timeout_seconds)` 中完整消费 Provider 流，把事件逐个放入 queue；异常也作为内部 queue item 返回。consumer 每次用 `asyncio.wait()` 竞争 `queue.get()` 与 `context.wait_cancelled()`，因此等待 LLM 时取消可以立即停止 producer。

轮次累加器必须保存：

```python
@dataclass(slots=True)
class _RoundData:
    text_parts: list[str] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    saw_thinking: bool = False
    turn_end: ProviderTurnEnd | None = None
```

事件映射固定为：

```python
ProviderThinkingDelta(text)    -> ModelThinkingEvent(text)
ProviderThinkingComplete(block)-> 只加入 thinking_blocks
ProviderTextDelta(text)        -> ModelTextEvent(text)
ProviderToolCall(tool_call)    -> 只加入 tool_calls
ProviderTurnEnd(reason)        -> 保存并校验它是最后一个 Provider 事件
```

producer 结束时没有 `ProviderTurnEnd`、结尾后还有事件、停止原因和 tool call 冲突、重复结束事件，都产生 `invalid_provider_stream`。`ProviderError` 产生 `provider_error` 并使用其已脱敏文本；TimeoutError 产生 `llm_timeout`。任何未完整结束的本轮 assistant 内容都不得写入历史。

- [ ] **Step 8: 实现 ReAct、历史提交和审批状态机**

`run()` 必须先校验 `user_message.strip()`，再调用 `context.begin_run()`，并在 `finally` 调用 `context.finish_run()`。运行局部状态：

```python
state: AgentRunState = "planning" if plan_only else "executing"
current_request_authorized = False
round_number = 0
```

`RoundStartedEvent.mode` 只从 `state` 为 `planning` 或 `executing` 时产生。等待最终计划时切换为 `waiting_plan_approval`；ToolScheduler 等待单工具批准时使用 `waiting_tool_approval` 语义；成功、取消、错误终止前分别切换为 `completed`、`cancelled`、`failed`。

每轮开始发 `RoundStartedEvent`；轮数小于 `max_rounds` 时传 `registry.api_tools(provider.protocol)`，等于 `max_rounds` 时传 `None`。

如果最终轮仍返回任何 `ProviderToolCall`，必须在写 assistant 工具调用历史或调用 scheduler 之前发 `RunErrorEvent(code="max_rounds_exceeded", ...)` 并终止。

工具轮按顺序执行：

```python
history.add_assistant_tool_calls(
    "".join(round_data.text_parts),
    tuple(round_data.tool_calls),
    thinking_blocks=tuple(round_data.thinking_blocks),
)
async for event in self._scheduler.run(
    tuple(round_data.tool_calls),
    plan_only=plan_only,
    current_request_authorized=current_request_authorized,
    context=context,
):
    if isinstance(event, ToolResultEvent):
        history.add_tool_result(event.call_id, event.result)
    yield event
```

调度结束且 `context.cancelled` 时发唯一的 `RunCancelledEvent("user_cancelled")`；否则进入下一轮。

无工具 executing 正文必须 `strip()` 后非空，先 `history.add_assistant(content)` 再发 `FinalResponseEvent(content, round_number)`。无任何内容为 `empty_response`；只有 thinking 为 `invalid_provider_stream`；`stop_reason == "max_tokens"` 为 `max_tokens_reached`。

无工具 planning 正文先写 assistant 历史，再 `open_plan_approval()` 并发 `PlanApprovalRequestedEvent`。第 `max_rounds` 轮两个能力标志均为 False，结束后固定 reason 为 `round_limit_after_plan`。其他轮按 Step 4 的三种选择修改 mode、反馈历史和局部授权。

- [ ] **Step 9: 运行 Agent 核心、Provider、调度器和历史测试**

Run: `uv run pytest tests/test_agent_loop.py tests/test_agent_context.py tests/test_tool_scheduler.py tests/test_history.py tests/test_openai_provider.py tests/test_anthropic_provider.py -v`

Expected: PASS；每个 AgentLoop 测试只有一个终止事件，15 轮、thinking 和取消历史断言全部通过。

- [ ] **Step 10: 提交 Task 7**

```powershell
git add -- src/mewcode_agent/agent/loop.py src/mewcode_agent/agent/__init__.py tests/test_agent_loop.py
git commit -m "Add ReAct agent loop"
```

---

### Task 8: Textual 审批卡片、事件消费与 CLI 组装

**Files:**

- Modify: `src/mewcode_agent/app.py`
- Modify: `src/mewcode_agent/cli.py`
- Replace Agent-loop-specific tests in: `tests/test_app.py`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Consumes: `AgentLoop.run()`、全部 `AgentEvent`、`AgentRunContext`
- Produces: `ToolApprovalScreen(ModalScreen[ToolApprovalDecision | None])`
- Produces: `PlanApprovalScreen(ModalScreen[PlanApprovalResolution | None])`
- Changes: `ChatApp(agent_loop, history, *, provider_id, model)`
- Removes: `MAX_TOOL_CALLS_PER_TURN`、`_stream_round()`、`_run_agent_loop()` 和 UI 内直接执行工具的路径

- [ ] **Step 1: 用 Fake AgentLoop 重写 UI 事件消费测试**

删除 `tests/test_app.py` 中对旧 `str | ToolCall` Provider、`MAX_TOOL_CALLS_PER_TURN` 和 UI 工具循环的依赖。定义一个只产 Agent 事件的 fake：

```python
class GatedAgentLoop:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.plan_only_values: list[bool] = []

    async def run(
        self,
        user_message: str,
        history: ConversationHistory,
        *,
        plan_only: bool,
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        context.begin_run()
        self.plan_only_values.append(plan_only)
        history.add_user(user_message)
        yield UserMessageEvent(user_message)
        yield RoundStartedEvent(1, 15, "planning" if plan_only else "executing")
        yield ModelThinkingEvent("分析")
        yield ModelTextEvent("分片")
        self.started.set()
        await self.release.wait()
        history.add_assistant("分片完成")
        yield ModelTextEvent("完成")
        yield FinalResponseEvent("分片完成", 1)
        context.finish_run()
```

主 UI 测试精确断言：输入在运行时禁用；日志显示 thinking 与正文；释放后 history 只有一份 user/assistant；输入恢复并聚焦；`Switch(id="plan-only-switch")` 的值被传给 loop，且请求结束后开关值不变。

- [ ] **Step 2: 写两类 Modal 卡片和取消回写失败测试**

为工具卡片写 fake loop：`context.begin_run()` → `open_tool_approval()` → yield `ToolApprovalRequestedEvent` → await `wait_for_tool_approval()`；测试用：

```python
await pilot.click("#allow-once")
assert fake_loop.tool_decision == "allow_once"
```

第二个测试点击 `#reject-tool`，断言 `reject`。

为计划卡片分别点击 `#execute-current`、填写 `Input(id="plan-feedback")` 后点击 `#request-changes`、点击 `#reject-plan`，断言 Context 收到：

```python
PlanApprovalResolution("execute_current", "")
PlanApprovalResolution("request_changes", "补充测试步骤")
PlanApprovalResolution("reject", "")
```

当事件的 `can_execute=False`、`can_request_changes=False` 时，断言前两个 Button 的 `disabled is True`，卡片包含：`当前请求已达到 15 轮上限，请开启新请求执行该计划。`

审批卡片打开时按 Escape，断言 active context 被取消、Modal 关闭且 fake loop 以取消路径结束。

- [ ] **Step 3: 运行 App 测试，确认构造函数和旧循环不匹配**

Run: `uv run pytest tests/test_app.py -v`

Expected: FAIL，现有 `ChatApp` 仍要求 Provider，并且不存在两个 Modal Screen 与 plan-only Switch。

- [ ] **Step 4: 实现工具审批 Modal**

在 `app.py` 定义：

```python
class ToolApprovalScreen(ModalScreen[ToolApprovalDecision | None]):
    BINDINGS = [("escape", "cancel_run", "取消当前请求")]

    def __init__(self, event: ToolApprovalRequestedEvent) -> None:
        super().__init__()
        self.event = event

    def compose(self) -> ComposeResult:
        yield Static(
            f"{self.event.tool_name}\n{self.event.arguments_json}",
            classes="approval-card",
        )
        yield Button("仅本次允许", id="allow-once", variant="primary")
        yield Button("拒绝", id="reject-tool", variant="error")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "allow-once":
            self.dismiss("allow_once")
        elif event.button.id == "reject-tool":
            self.dismiss("reject")

    def action_cancel_run(self) -> None:
        self.app.action_cancel_run()
        self.dismiss(None)
```

Modal CSS 必须覆盖全屏半透明背景，并把 `.approval-card` 呈现为居中、有边框的卡片。

- [ ] **Step 5: 实现计划审批 Modal**

定义：

```python
class PlanApprovalScreen(ModalScreen[PlanApprovalResolution | None]):
    BINDINGS = [("escape", "cancel_run", "取消当前请求")]

    def __init__(self, event: PlanApprovalRequestedEvent) -> None:
        super().__init__()
        self.event = event

    def compose(self) -> ComposeResult:
        yield Static(self.event.plan, classes="approval-card")
        if not self.event.can_execute:
            yield Static("当前请求已达到 15 轮上限，请开启新请求执行该计划。")
        yield Input(id="plan-feedback", placeholder="修改计划时填写反馈")
        yield Button(
            "执行当前计划",
            id="execute-current",
            variant="primary",
            disabled=not self.event.can_execute,
        )
        yield Button(
            "修改计划",
            id="request-changes",
            disabled=not self.event.can_request_changes,
        )
        yield Button("拒绝并结束", id="reject-plan", variant="error")
```

按钮 handler：execute 返回 `PlanApprovalResolution("execute_current")`；reject 返回 `PlanApprovalResolution("reject")`；request changes 读取 `#plan-feedback`，空白时不关闭卡片并把 placeholder 改为 `必须填写修改意见`，非空时返回 `PlanApprovalResolution("request_changes", feedback.strip())`。Escape 与工具卡片相同，先取消 active context 再 `dismiss(None)`。

- [ ] **Step 6: 把 `ChatApp` 改为纯 Agent 事件消费者**

构造函数改为：

```python
def __init__(
    self,
    agent_loop: AgentLoop,
    history: ConversationHistory,
    *,
    provider_id: str,
    model: str,
) -> None:
    super().__init__()
    self.agent_loop = agent_loop
    self.history = history
    self.provider_id = provider_id
    self.model = model
    self.active_response = ""
    self.active_thinking = ""
    self._active_context: AgentRunContext | None = None
```

`compose()` 在输入框前加入 `Switch(id="plan-only-switch", value=False)`。`submit_prompt()` 不再写 history，只清空输入、禁用 Input/Switch，并调用 `stream_response(prompt)`。

`_render_transcript()` 继续从 `history.snapshot()` 渲染已提交消息，并在末尾按以下顺序渲染临时过程，避免 thinking 混入 assistant 正文：

```python
if self.active_thinking:
    log.write(f"Thinking: {self.active_thinking}")
if self.active_response:
    log.write(f"Assistant: {self.active_response}")
```

worker 主体：

```python
context = AgentRunContext()
self._active_context = context
try:
    async for event in self.agent_loop.run(
        prompt,
        self.history,
        plan_only=plan_only_switch.value,
        context=context,
    ):
        await self._handle_agent_event(event, context)
finally:
    self._active_context = None
    prompt_input.disabled = False
    plan_only_switch.disabled = False
    prompt_input.focus()
```

`_handle_agent_event()` 的准确职责：

- `ModelThinkingEvent` 追加 `active_thinking` 并重绘。
- `ModelTextEvent` 追加 `active_response` 并重绘。
- `ToolApprovalRequestedEvent` 调 `await self.push_screen_wait(ToolApprovalScreen(event))`；结果非 None 时调用 `context.resolve_tool_approval()`。
- `PlanApprovalRequestedEvent` 同样显示计划卡片并调用 `context.resolve_plan_approval()`。
- `ToolCallStartedEvent` 和 `RoundStartedEvent` 更新 status。
- `ToolResultEvent` 重绘已经由 AgentLoop 写入的 history。
- `FinalResponseEvent` 清空临时分片、重绘并设状态 `就绪`。
- `RunErrorEvent` 清空临时分片并设状态 `错误：{event.message}`。
- `RunCancelledEvent` 清空临时分片并设状态 `已取消：{event.reason}`。

添加 App binding `("escape", "cancel_run", "取消当前请求")`；`action_cancel_run()` 只在 `_active_context is not None` 时调用 `cancel()`。删除 `_stream_round()`、`_run_agent_loop()`、Provider/ToolRegistry 直接依赖和 `MAX_TOOL_CALLS_PER_TURN`。

- [ ] **Step 7: 更新 CLI 组装和 CLI 测试**

`cli.main()` 成功路径改为：

```python
registry = create_core_registry()
agent_loop = AgentLoop(provider, registry)
app = ChatApp(
    agent_loop,
    ConversationHistory(),
    provider_id=provider_config.provider_id,
    model=provider_config.model,
)
```

`tests/test_cli.py::test_cli_builds_and_runs_app_with_valid_config` 保留启动成功断言，并 monkeypatch `cli.AgentLoop` 记录它收到的 provider 和 registry，确认 CLI 只组装一次。

- [ ] **Step 8: 运行 TUI、CLI 和 Agent 回归测试**

Run: `uv run pytest tests/test_app.py tests/test_cli.py tests/test_agent_loop.py -v`

Expected: PASS；`tests/test_app.py` 不再定义字符串 Provider 或直接执行 ToolRegistry。

- [ ] **Step 9: 提交 Task 8**

```powershell
git add -- src/mewcode_agent/app.py src/mewcode_agent/cli.py tests/test_app.py tests/test_cli.py
git commit -m "Connect Textual UI to agent events"
```

---

### Task 9: 全量回归与章节验收记录

**Files:**

- Modify: `docs/ch02/checklist.md`
- Modify: `docs/ch02/tasks.md`
- Modify: `docs/ch02/spec.md`

**Interfaces:**

- Verifies: `docs/ch02/spec.md` 第 21 节全部 20 条验收标准
- Produces: 可复查的本地命令、结果和最终 commit 记录

- [ ] **Step 1: 运行禁止 UI 依赖检查**

Run: `rg -n "textual" src/mewcode_agent/agent`

Expected: exit code `1` 且没有输出，证明 Agent 核心没有导入 Textual。

- [ ] **Step 2: 运行旧循环残留检查**

Run: `rg -n "MAX_TOOL_CALLS_PER_TURN|_run_agent_loop|_stream_round" src tests`

Expected: exit code `1` 且没有输出。

- [ ] **Step 3: 运行编译检查**

Run: `uv run python -m compileall -q src tests`

Expected: exit code `0`，没有语法错误输出。

- [ ] **Step 4: 运行全量离线测试**

Run: `uv run pytest -m "not integration"`

Expected: exit code `0`，报告 `0 failed`、`0 errors`，并且不访问网络或读取真实 API Key。

- [ ] **Step 5: 运行完整默认测试命令**

Run: `uv run pytest`

Expected: exit code `0`，报告 `0 failed`、`0 errors`。

- [ ] **Step 6: 检查补丁格式与规格占位符**

Run: `git diff --check`

Expected: exit code `0`。

Run: `$tokens = @(('T' + 'BD'), ('T' + 'ODO'), ('FIX' + 'ME'), ('待' + '定'), ('待' + '补充')); $matches = Select-String -Path 'docs/ch02/*.md' -Pattern $tokens; if ($matches) { $matches; exit 1 }`

Expected: exit code `0` 且没有输出。

- [ ] **Step 7: 按真实输出填写章节文档**

在 `docs/ch02/checklist.md` 勾选每条已经由测试或静态检查证明的验收项，并在“验证证据”表填写实际命令、exit code、测试统计和 commit hash。在 `docs/ch02/tasks.md` 勾选 9 个已完成任务。把 `docs/ch02/spec.md` 文档状态更新为：

```text
- 状态：实现完成并通过 Chapter 02 验收
- 实现授权：已执行
```

不得预填测试数量、commit hash 或运行日期；这些值只能从本步骤刚运行的输出复制。

- [ ] **Step 8: 提交最终验收记录**

```powershell
git add -- docs/ch02/spec.md docs/ch02/tasks.md docs/ch02/checklist.md
git commit -m "Complete Chapter 02 verification"
```

- [ ] **Step 9: 确认工作树状态**

Run: `git status --short`

Expected: 没有输出。此步骤不执行 push；只有用户明确要求时才推送。
