"""Run one worker through the existing AgentLoop."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from contextvars import ContextVar
from hashlib import sha256
import re

from mewcode_agent.agent.context import AgentRunContext
from mewcode_agent.agent.events import (
    FinalResponseEvent,
    RunCancelledEvent,
    RunErrorEvent,
    ToolApprovalRequestedEvent,
)
from mewcode_agent.agent.loop import AgentLoop, AgentLoopConfig
from mewcode_agent.agent.tool_scheduler import ToolScheduler
from mewcode_agent.compaction import ContextWindowManager, history_atomic_boundaries
from mewcode_agent.history import ConversationHistory
from mewcode_agent.hooks import HookEngine, HookToolExecutionInterceptor, PromptHookBridge
from mewcode_agent.models import ChatMessage
from mewcode_agent.prompting.composer import PromptComposer
from mewcode_agent.prompting.models import RuntimeInstruction
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.providers.base import LLMProvider
from mewcode_agent.security.policy import SecurityPolicyEngine
from mewcode_agent.skills.runtime import SkillRuntime
from mewcode_agent.skills.tools import LoadSkillTool
from mewcode_agent.tools.registry import ToolRegistry
from mewcode_agent.workers.models import (
    WorkerError,
    WorkerExecutionOutcome,
    WorkerExecutionSpec,
    WorkerPermissionMode,
    WorkerRoleDefinition,
    WorkerRuntimeConfig,
)
from mewcode_agent.workers.usage import WorkerUsageCollector


ProviderResolver = Callable[[str], LLMProvider]
PolicyEngineFactory = Callable[
    [WorkerPermissionMode], SecurityPolicyEngine | None
]
ContextManagerFactory = Callable[
    [LLMProvider], ContextWindowManager | None
]


_FORK_HEADINGS = (
    "## Summary",
    "## Evidence",
    "## Risks",
    "## Next Steps",
)
_WORKER_EXECUTION_ACTIVE: ContextVar[bool] = ContextVar(
    "mewcode_worker_execution_active",
    default=False,
)


def worker_execution_active() -> bool:
    return _WORKER_EXECUTION_ACTIVE.get()


def fork_history_prefix(
    messages: tuple[ChatMessage, ...],
) -> tuple[ChatMessage, ...]:
    """Drop the current incomplete tool batch and validate the prefix."""

    last_tool_assistant: int | None = None
    for index, message in enumerate(messages):
        if message.role == "assistant" and message.tool_calls:
            last_tool_assistant = index
    result = messages
    if last_tool_assistant is not None:
        assistant = messages[last_tool_assistant]
        expected = tuple(call.call_id for call in assistant.tool_calls)
        following = messages[
            last_tool_assistant + 1 : last_tool_assistant + 1 + len(expected)
        ]
        actual = tuple(
            message.tool_call_id
            for message in following
            if message.role == "tool"
        )
        if len(following) != len(expected) or actual != expected:
            result = messages[:last_tool_assistant]
    history_atomic_boundaries(list(result))
    return result


def visible_worker_tools(
    registry_tool_names: tuple[str, ...],
    *,
    base_visible_tools: frozenset[str] | None,
    definition: WorkerRoleDefinition | None,
    background: bool,
    runtime_config: WorkerRuntimeConfig,
) -> frozenset[str]:
    base = (
        frozenset(registry_tool_names)
        if base_visible_tools is None
        else base_visible_tools
    )
    allowed = set(base)
    allowed.discard("spawn_worker")
    if definition is not None:
        if definition.allowed_tools is not None:
            allowed.intersection_update(definition.allowed_tools)
        allowed.difference_update(definition.denied_tools)
    if background:
        allowed.intersection_update(runtime_config.background_allowed_tools)
    return frozenset(name for name in registry_tool_names if name in allowed)


def fork_report_format_valid(content: str) -> bool:
    if len(content) > 1200:
        return False
    positions: list[int] = []
    for heading in _FORK_HEADINGS:
        matches = tuple(re.finditer(rf"(?m)^{re.escape(heading)}$", content))
        if len(matches) != 1:
            return False
        positions.append(matches[0].start())
    return positions == sorted(positions)


def definition_user_prompt(task: str) -> str:
    return (
        "执行下列子工作者任务。不要向用户提问，不要请求额外输入；"
        "在现有权限和工具范围内直接完成。没有更多工具需要调用时，"
        "输出最终结果并结束。\n\n任务（原文）：\n"
        f"{task}"
    )


def fork_user_prompt(task: str) -> str:
    return (
        "执行下列 Fork 子工作者任务。\n"
        "1. 不能调用 spawn_worker 或创建下一层 worker。\n"
        "2. 不得主动对话、提问或请求确认。\n"
        "3. 直接使用可见工具完成任务，遇到被拒绝工具时调整方案。\n"
        "4. 没有工具调用时必须结束。\n"
        "5. 最终报告不超过 1200 个 Unicode code point。\n"
        "6. 最终报告按精确 Markdown 标题 ## Summary、## Evidence、"
        "## Risks、## Next Steps 排列。\n\n任务（原文）：\n"
        f"{task}"
    )


def _role_control(definition: WorkerRoleDefinition, task: str) -> RuntimeInstruction:
    digest = sha256(definition.name.encode("utf-8")).hexdigest()[:24]
    return RuntimeInstruction(
        f"runtime.workers.role_{digest}",
        "context",
        "session",
        (
            f"Worker role: {definition.name}\n"
            f"Task boundary:\n{task}\n"
            f"SOP:\n{definition.body}"
        ),
        "worker",
    )


class WorkerExecutor:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        parent_prompt_runtime: PromptRuntime,
        prompt_composer: PromptComposer,
        provider_resolver: ProviderResolver,
        policy_engine_factory: PolicyEngineFactory,
        context_manager_factory: ContextManagerFactory,
        hook_engine: HookEngine | None = None,
        prompt_hook_bridge: PromptHookBridge | None = None,
        skill_runtime: SkillRuntime | None = None,
        load_skill_tool: LoadSkillTool | None = None,
    ) -> None:
        if (skill_runtime is None) != (load_skill_tool is None):
            raise ValueError(
                "skill_runtime 与 load_skill_tool 必须同时提供或省略"
            )
        self._registry = registry
        self._parent_prompt_runtime = parent_prompt_runtime
        self._prompt_composer = prompt_composer
        self._provider_resolver = provider_resolver
        self._policy_engine_factory = policy_engine_factory
        self._context_manager_factory = context_manager_factory
        self._hook_engine = hook_engine
        self._prompt_hook_bridge = prompt_hook_bridge
        self._skill_runtime = skill_runtime
        self._load_skill_tool = load_skill_tool
        self._contexts: dict[str, AgentRunContext] = {}

    def cancel(self, task_id: str) -> bool:
        context = self._contexts.get(task_id)
        if context is None:
            return False
        context.cancel()
        return True

    async def run(
        self,
        spec: WorkerExecutionSpec,
        usage: WorkerUsageCollector,
    ) -> WorkerExecutionOutcome:
        provider = self._provider_resolver(spec.provider_id)
        definition = spec.definition
        controls = () if definition is None else (_role_control(definition, spec.task),)
        runtime = self._parent_prompt_runtime.fork_current_session(
            extra_controls=controls
        )
        history = ConversationHistory()
        if spec.parent_history:
            history.restore(fork_history_prefix(spec.parent_history))
        permission_mode: WorkerPermissionMode = (
            definition.permission_mode if definition is not None else "inherit"
        )
        interceptor = (
            HookToolExecutionInterceptor(self._hook_engine)
            if self._hook_engine is not None
            else None
        )
        scheduler = ToolScheduler(
            self._registry,
            interceptor=interceptor,
            policy_engine=self._policy_engine_factory(permission_mode),
        )
        loop = AgentLoop(
            provider,
            self._registry,
            prompt_runtime=runtime,
            prompt_composer=self._prompt_composer,
            config=AgentLoopConfig(
                max_rounds=(definition.max_rounds if definition is not None else 15)
            ),
            scheduler=scheduler,
            usage_collector=usage,
            context_window_manager=self._context_manager_factory(provider),
            visible_tool_names=lambda: spec.visible_tools,
            hook_engine=self._hook_engine,
        )
        context = AgentRunContext()
        self._contexts[spec.task_id] = context
        final: str | None = None
        prompt = (
            fork_user_prompt(spec.task)
            if definition is None
            else definition_user_prompt(spec.task)
        )
        cache_binding = (
            self._registry.file_state_cache.isolated()
            if self._registry.file_state_cache is not None
            else nullcontext()
        )
        try:
            with ExitStack() as stack:
                worker_token = _WORKER_EXECUTION_ACTIVE.set(True)
                stack.callback(_WORKER_EXECUTION_ACTIVE.reset, worker_token)
                stack.enter_context(cache_binding)
                if self._prompt_hook_bridge is not None:
                    stack.enter_context(
                        self._prompt_hook_bridge.bind_runtime(
                            runtime,
                            history_length_provider=lambda: len(history),
                        )
                    )
                if (
                    self._skill_runtime is not None
                    and self._load_skill_tool is not None
                ):
                    worker_skill_runtime = self._skill_runtime.fork_current(
                        runtime
                    )
                    stack.enter_context(
                        self._load_skill_tool.bind_runtime(
                            worker_skill_runtime
                        )
                    )
                async for event in loop.run(
                    prompt,
                    history,
                    plan_only=False,
                    context=context,
                ):
                    if isinstance(event, ToolApprovalRequestedEvent):
                        context.resolve_tool_approval(event.request_id, "reject")
                    elif isinstance(event, FinalResponseEvent):
                        final = event.content
                    elif isinstance(event, RunErrorEvent):
                        raise WorkerError(event.code, "Worker AgentLoop 执行失败")
                    elif isinstance(event, RunCancelledEvent):
                        raise WorkerError("worker_cancelled", "Worker 已取消")
        except WorkerError:
            raise
        except Exception as exc:
            raise WorkerError("worker_failed", "Worker 执行失败") from exc
        finally:
            self._contexts.pop(spec.task_id, None)
        if final is None:
            raise WorkerError("worker_failed", "Worker 未返回最终结果")
        return WorkerExecutionOutcome(
            final,
            fork_report_format_valid(final) if definition is None else True,
        )
