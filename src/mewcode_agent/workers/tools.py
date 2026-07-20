"""Single Tool adapter for definition and Fork workers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from uuid import uuid4

from mewcode_agent.history import ConversationHistory
from mewcode_agent.tools.base import Tool, ToolExecutionError, validate_arguments
from mewcode_agent.tools.registry import ToolRegistry
from mewcode_agent.workers.catalog import WorkerCatalog
from mewcode_agent.workers.executor import visible_worker_tools
from mewcode_agent.workers.manager import WorkerManager
from mewcode_agent.workers.models import (
    WORKER_NAME_PATTERN,
    WorkerError,
    WorkerExecutionSpec,
)


class SpawnWorkerTool(Tool):
    name = "spawn_worker"
    category = "read"
    timeout_seconds = 300.0
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "type": {"type": "string"},
            "background": {"type": "boolean"},
        },
        "required": ["task"],
        "additionalProperties": False,
    }

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
    ) -> None:
        self._catalog = catalog
        self._manager = manager
        self._registry = registry
        self._main_history = main_history
        self._session_id_provider = session_id_provider
        self._parent_visible_tools = parent_visible_tools
        self._parent_provider_id = parent_provider_id
        self._provider_models = dict(provider_models)
        if parent_provider_id not in self._provider_models:
            raise ValueError("parent_provider_id 不存在于 provider_models")
        missing_models = tuple(
            definition.model
            for definition in catalog.snapshot.definitions
            if definition.model != "inherit"
            and definition.model not in self._provider_models
        )
        if missing_models:
            raise ValueError("Worker catalog 引用了未提供的 Provider model")
        roles = "; ".join(
            f"{item.name}: {item.description}"
            for item in catalog.snapshot.definitions
        )
        self.description = (
            "启动一个不可嵌套的子工作者。省略 type 时创建强制后台 Fork。"
            f"可用角色：{roles}"
        )

    async def execute(self, arguments: dict[str, object]) -> object:
        validate_arguments(
            arguments,
            required={"task": str},
            optional={"type": str, "background": bool},
        )
        task = arguments["task"]
        assert isinstance(task, str)
        if not task.strip() or len(task) > 32768:
            raise ToolExecutionError(
                "invalid_arguments",
                "task 必须是 1..32768 code points 的非空字符串",
            )
        raw_type = arguments.get("type")
        if raw_type is not None and (
            not raw_type or WORKER_NAME_PATTERN.fullmatch(raw_type) is None
        ):
            raise ToolExecutionError("invalid_arguments", "type 格式无效")
        definition = (
            None if raw_type is None else self._catalog.get(raw_type)
        )
        if raw_type is not None and definition is None:
            raise ToolExecutionError(
                "worker_type_not_found",
                "Worker type 不存在",
            )
        fork = raw_type is None
        background_requested = bool(arguments.get("background", False))
        background = fork or background_requested
        provider_id = (
            self._parent_provider_id
            if definition is None or definition.model == "inherit"
            else definition.model
        )
        model = self._provider_models[provider_id]
        visible = visible_worker_tools(
            self._registry.tool_names(),
            base_visible_tools=(
                self._parent_visible_tools() if fork else None
            ),
            definition=definition,
            background=background,
            runtime_config=self._catalog.snapshot.runtime_config,
        )
        spec = WorkerExecutionSpec(
            uuid4().hex,
            self._session_id_provider(),
            "fork" if fork else raw_type,
            "fork" if fork else "definition",
            task,
            definition,
            tuple(self._main_history.snapshot()) if fork else (),
            visible,
            provider_id,
            model,
        )
        transition = (
            "fork_forced" if fork else "explicit" if background else None
        )
        try:
            snapshot = await self._manager.start(
                spec,
                background=background,
                transition=transition,
            )
            if background:
                return {
                    "task_id": snapshot.task_id,
                    "status": "running",
                    "mode": "background",
                    "type": snapshot.worker_type,
                    "transition": snapshot.transition,
                    "workspace": (
                        None
                        if snapshot.workspace is None
                        else snapshot.workspace.to_dict()
                    ),
                }
            snapshot = await self._manager.wait_foreground(snapshot.task_id)
        except WorkerError as exc:
            raise ToolExecutionError(exc.code, exc.message) from exc
        if snapshot.mode == "background":
            return {
                "task_id": snapshot.task_id,
                "status": snapshot.state,
                "mode": "background",
                "type": snapshot.worker_type,
                "transition": snapshot.transition,
                "workspace": (
                    None
                    if snapshot.workspace is None
                    else snapshot.workspace.to_dict()
                ),
            }
        if snapshot.state != "completed" or snapshot.result is None:
            raise ToolExecutionError(
                "worker_failed",
                "Worker 未成功完成",
                details={
                    "workspace": (
                        None
                        if snapshot.workspace is None
                        else snapshot.workspace.to_dict()
                    )
                },
            )
        return {
            "task_id": snapshot.task_id,
            "status": "completed",
            "mode": "foreground",
            "type": snapshot.worker_type,
            "result": snapshot.result,
            "usage": snapshot.usage.to_dict(),
            "workspace": (
                None
                if snapshot.workspace is None
                else snapshot.workspace.to_dict()
            ),
        }
