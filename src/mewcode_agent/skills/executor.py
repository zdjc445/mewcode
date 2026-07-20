"""Independent Agent execution for isolated Skills."""

from __future__ import annotations

from collections.abc import Callable

from mewcode_agent.agent.context import AgentRunContext
from mewcode_agent.agent.events import (
    FinalResponseEvent,
    RunCancelledEvent,
    RunErrorEvent,
    ToolApprovalRequestedEvent,
)
from mewcode_agent.agent.loop import AgentLoop, AgentLoopConfig
from mewcode_agent.agent.tool_scheduler import ToolScheduler
from mewcode_agent.compaction import (
    CONTEXT_BOUNDARY_TEXT,
    ContextCompactionError,
    ContextSummarizer,
    SummaryCheckpoint,
    VerbatimUserMessage,
    history_atomic_boundaries,
)
from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage
from mewcode_agent.prompting.composer import PromptComposer
from mewcode_agent.prompting.models import (
    ContextBoundaryMessage,
    ContextSummaryMessage,
    PromptFrame,
    PromptItem,
)
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.providers.base import LLMProvider
from mewcode_agent.skills.models import SkillConfigError, SkillDefinition
from mewcode_agent.skills.runtime import SkillRuntime
from mewcode_agent.skills.tools import LoadSkillTool
from mewcode_agent.tools.registry import ToolRegistry


IsolatedApprovalHandler = Callable[
    [ToolApprovalRequestedEvent, AgentRunContext], None
]


def reject_isolated_approval(
    event: ToolApprovalRequestedEvent,
    context: AgentRunContext,
) -> None:
    """Reject an isolated approval that cannot be relayed through Tool."""

    context.resolve_tool_approval(event.request_id, "reject")


class _PrefixedPromptComposer:
    def __init__(
        self,
        base: PromptComposer,
        prefix: tuple[PromptItem, ...],
    ) -> None:
        self._base = base
        self._prefix = prefix

    def compose(self, history, timeline) -> PromptFrame:
        frame = self._base.compose(history, timeline)
        return PromptFrame(
            frame.system_prompt,
            (*self._prefix, *frame.items),
        )


class IsolatedSkillExecutor:
    """Run an isolated Skill and return only its final response text."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        registry: ToolRegistry,
        scheduler: ToolScheduler,
        prompt_runtime: PromptRuntime,
        prompt_composer: PromptComposer,
        skill_runtime: SkillRuntime,
        load_skill_tool: LoadSkillTool,
        main_history: ConversationHistory,
        summarizer: ContextSummarizer,
        approval_handler: IsolatedApprovalHandler,
        loop_config: AgentLoopConfig | None = None,
    ) -> None:
        if not callable(approval_handler):
            raise ValueError("approval_handler 必须可调用")
        self._provider = provider
        self._registry = registry
        self._scheduler = scheduler
        self._prompt_runtime = prompt_runtime
        self._prompt_composer = prompt_composer
        self._skill_runtime = skill_runtime
        self._load_skill_tool = load_skill_tool
        self._main_history = main_history
        self._summarizer = summarizer
        self._approval_handler = approval_handler
        self._loop_config = loop_config or AgentLoopConfig()

    async def run(self, definition: SkillDefinition, arguments: str) -> str:
        if definition.execution_mode != "isolated":
            raise SkillConfigError(
                "skill_isolated_failed",
                "只有 isolated Skill 可以使用隔离执行器",
            )
        try:
            prefix, messages = await self._context_input(definition)
        except SkillConfigError:
            raise
        except Exception as exc:
            raise SkillConfigError(
                "skill_isolated_failed",
                "隔离 Skill 上下文准备失败",
            ) from exc
        history = ConversationHistory()
        history.restore(messages)
        isolated_prompt_runtime = self._prompt_runtime.fork()
        isolated_skill_runtime = self._skill_runtime.fork(
            isolated_prompt_runtime
        )
        isolated_skill_runtime.prime_isolated(definition, arguments)
        loop = AgentLoop(
            self._provider,
            self._registry,
            prompt_runtime=isolated_prompt_runtime,
            prompt_composer=_PrefixedPromptComposer(
                self._prompt_composer,
                prefix,
            ),  # type: ignore[arg-type]
            config=self._loop_config,
            scheduler=self._scheduler,
            context_window_manager=None,
            visible_tool_names=isolated_skill_runtime.visible_tool_names,
        )
        context = AgentRunContext()
        final: str | None = None
        prompt = (
            f"执行 Skill `{definition.name}`。\n"
            "Skill 参数（原文）：\n"
            f"{arguments}\n"
            "严格遵循环境上下文中的 Skill SOP。"
            "完成后直接输出需要回流主对话的最终结果摘要。"
        )
        try:
            with self._load_skill_tool.bind_runtime(isolated_skill_runtime):
                async for event in loop.run(
                    prompt,
                    history,
                    plan_only=False,
                    context=context,
                ):
                    if isinstance(event, ToolApprovalRequestedEvent):
                        self._approval_handler(event, context)
                    elif isinstance(event, FinalResponseEvent):
                        final = event.content
                    elif isinstance(event, (RunErrorEvent, RunCancelledEvent)):
                        raise SkillConfigError(
                            "skill_isolated_failed",
                            "隔离 Skill 未成功完成",
                        )
        except SkillConfigError:
            raise
        except Exception as exc:
            raise SkillConfigError(
                "skill_isolated_failed",
                "隔离 Skill 执行失败",
            ) from exc
        if final is None:
            raise SkillConfigError(
                "skill_isolated_failed",
                "隔离 Skill 未返回最终结果",
            )
        return final

    async def _context_input(
        self,
        definition: SkillDefinition,
    ) -> tuple[tuple[PromptItem, ...], tuple[ChatMessage, ...]]:
        messages = self._main_history.snapshot()
        if definition.context_strategy == "none" or not messages:
            return (), ()
        if definition.context_strategy == "recent":
            assert definition.recent_messages is not None
            boundaries = (0, *history_atomic_boundaries(messages))
            target = max(0, len(messages) - definition.recent_messages)
            start = max(boundary for boundary in boundaries if boundary <= target)
            return (), tuple(messages[start:])
        if definition.context_strategy != "summary":
            raise SkillConfigError(
                "skill_isolated_failed",
                "隔离 Skill 上下文策略无效",
            )
        try:
            generated = await self._summarizer.summarize(
                previous=None,
                history_start=0,
                history_end=len(messages),
                messages=tuple(messages),
            )
        except ContextCompactionError as exc:
            raise SkillConfigError(
                "skill_isolated_failed",
                "隔离 Skill 上下文摘要失败",
            ) from exc
        checkpoint = SummaryCheckpoint(
            1,
            len(messages),
            generated.sections,
            tuple(
                VerbatimUserMessage(index, message.content)
                for index, message in enumerate(messages)
                if message.role == "user"
            ),
        )
        return (
            (
                ContextSummaryMessage(1, len(messages), checkpoint.to_json()),
                ContextBoundaryMessage(1, CONTEXT_BOUNDARY_TEXT),
            ),
            (),
        )
