"""Hook subagent context mapping onto background workers."""

from __future__ import annotations

from collections.abc import Callable, Mapping

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
from mewcode_agent.tools.registry import ToolRegistry
from mewcode_agent.workers.catalog import WorkerCatalog
from mewcode_agent.workers.executor import (
    fork_history_prefix,
    visible_worker_tools,
    worker_execution_active,
)
from mewcode_agent.workers.manager import WorkerManager
from mewcode_agent.workers.models import WorkerError, WorkerExecutionSpec


class HookSubagentLauncher:
    def __init__(
        self,
        *,
        catalog: WorkerCatalog,
        manager: WorkerManager,
        registry: ToolRegistry,
        main_history: ConversationHistory,
        session_id_provider: Callable[[], str],
        parent_visible_tools: Callable[[], frozenset[str] | None],
        parent_provider_id: str,
        provider_models: Mapping[str, str],
        summarizer: ContextSummarizer,
    ) -> None:
        self._catalog = catalog
        self._manager = manager
        self._registry = registry
        self._main_history = main_history
        self._session_id_provider = session_id_provider
        self._parent_visible_tools = parent_visible_tools
        self._parent_provider_id = parent_provider_id
        self._provider_models = dict(provider_models)
        self._summarizer = summarizer

    async def launch(self, task: str, context: str) -> None:
        if worker_execution_active():
            raise WorkerError(
                "worker_nesting_denied",
                "Worker 内不能创建下一层 Worker",
            )
        if context not in ("none", "recent", "summary"):
            raise ValueError("Hook subagent context 无效")
        if not isinstance(task, str) or not task.strip() or len(task) > 32768:
            raise WorkerError("worker_failed", "Hook subagent task 无效")
        source = fork_history_prefix(tuple(self._main_history.snapshot()))
        if context == "recent":
            definition = None
            parent_history = self._recent(source, 12)
            worker_type = "fork"
            provider_id = self._parent_provider_id
            base_visible = self._parent_visible_tools()
        else:
            definition = self._catalog.get("general")
            if definition is None:
                raise WorkerError(
                    "worker_type_not_found",
                    "general Worker type 不存在",
                )
            if definition.isolation == "worktree":
                raise WorkerError(
                    "worker_isolation_unavailable",
                    "worktree 隔离将在 Chapter 12 接入",
                )
            worker_type = definition.name
            provider_id = (
                self._parent_provider_id
                if definition.model == "inherit"
                else definition.model
            )
            base_visible = None
            parent_history = (
                await self._summary_history(source)
                if context == "summary"
                else ()
            )
        visible = visible_worker_tools(
            self._registry.tool_names(),
            base_visible_tools=base_visible,
            definition=definition,
            background=True,
            runtime_config=self._catalog.snapshot.runtime_config,
        )
        from uuid import uuid4

        spec = WorkerExecutionSpec(
            uuid4().hex,
            self._session_id_provider(),
            worker_type,
            "hook",
            task,
            definition,
            parent_history,
            visible,
            provider_id,
            self._provider_models[provider_id],
        )
        await self._manager.start(
            spec,
            background=True,
            transition="explicit",
        )

    @staticmethod
    def _recent(
        messages: tuple[ChatMessage, ...],
        count: int,
    ) -> tuple[ChatMessage, ...]:
        if not messages:
            return ()
        boundaries = (0, *history_atomic_boundaries(list(messages)))
        target = max(0, len(messages) - count)
        start = max(boundary for boundary in boundaries if boundary <= target)
        return messages[start:]

    async def _summary_history(
        self,
        messages: tuple[ChatMessage, ...],
    ) -> tuple[ChatMessage, ...]:
        if not messages:
            return ()
        try:
            generated = await self._summarizer.summarize(
                previous=None,
                history_start=0,
                history_end=len(messages),
                messages=messages,
            )
        except ContextCompactionError as exc:
            raise WorkerError(
                "worker_failed",
                "Hook subagent 上下文摘要失败",
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
            ChatMessage(
                "user",
                "父会话结构化摘要：\n"
                f"{checkpoint.to_json()}\n\n{CONTEXT_BOUNDARY_TEXT}",
            ),
        )
