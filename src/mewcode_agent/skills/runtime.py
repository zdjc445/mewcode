"""Active Skill controls and exact per-run tool visibility."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
import asyncio
import hashlib
import json
from typing import Any

from mewcode_agent.prompting.models import RuntimeInstruction
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.skills.catalog import SkillCatalog
from mewcode_agent.skills.models import (
    SkillCatalogSnapshot,
    SkillConfigError,
    SkillDefinition,
)
from mewcode_agent.skills.tools import build_skill_script_tools
from mewcode_agent.tools.registry import ToolRegistry


IsolatedSkillRunner = Callable[[SkillDefinition, str], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ActiveSkill:
    definition: SkillDefinition
    arguments: str
    isolated_root: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.definition, SkillDefinition):
            raise ValueError("definition 类型无效")
        if not isinstance(self.arguments, str) or "\x00" in self.arguments:
            raise ValueError("arguments 无效")
        if type(self.isolated_root) is not bool:
            raise ValueError("isolated_root 必须是 bool")


def _active_instruction_id(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    return f"runtime.skills.active_{digest}"


class SkillRuntime:
    """Own catalog activation state and synchronize Prompt controls."""

    def __init__(
        self,
        catalog: SkillCatalog,
        registry: ToolRegistry,
        prompt_runtime: PromptRuntime,
        *,
        reserved_command_names: Iterable[str],
        install_tools: bool = True,
    ) -> None:
        if not isinstance(catalog, SkillCatalog):
            raise ValueError("catalog 类型无效")
        if not isinstance(registry, ToolRegistry):
            raise ValueError("registry 类型无效")
        if not isinstance(prompt_runtime, PromptRuntime):
            raise ValueError("prompt_runtime 类型无效")
        self._catalog = catalog
        self._registry = registry
        self._prompt_runtime = prompt_runtime
        self._reserved_command_names = frozenset(reserved_command_names)
        self._active: OrderedDict[str, ActiveSkill] = OrderedDict()
        self._isolated_runner: IsolatedSkillRunner | None = None
        self._operation_lock = asyncio.Lock()
        if install_tools:
            self._install_snapshot(catalog.snapshot)
        self._sync_controls()

    @property
    def catalog(self) -> SkillCatalog:
        return self._catalog

    @property
    def active_skills(self) -> tuple[ActiveSkill, ...]:
        return tuple(self._active.values())

    def set_isolated_runner(self, runner: IsolatedSkillRunner) -> None:
        if not callable(runner):
            raise ValueError("runner 必须可调用")
        self._isolated_runner = runner

    def reset_session(self) -> None:
        self._active.clear()
        self._sync_controls()

    def visible_tool_names(self) -> frozenset[str]:
        all_names = frozenset(self._registry.tool_names())
        dedicated_names = {
            tool.name
            for definition in self._catalog.snapshot.definitions
            for tool in definition.dedicated_tools
        }
        if not self._active:
            return frozenset(
                name
                for name in all_names
                if name not in dedicated_names or name == "load_skill"
            )
        allowed: set[str] | None = None
        for active in self._active.values():
            current = set(active.definition.allowed_tools)
            allowed = current if allowed is None else allowed & current
        assert allowed is not None
        allowed.add("load_skill")
        return frozenset(name for name in all_names if name in allowed)

    async def load(self, name: str, arguments: str) -> dict[str, Any]:
        if not isinstance(name, str) or not name:
            raise SkillConfigError("skill_not_found", "Skill 不存在")
        if not isinstance(arguments, str) or "\x00" in arguments:
            raise SkillConfigError("skill_activation_failed", "Skill arguments 无效")
        async with self._operation_lock:
            return await self._load_locked(name, arguments)

    async def _load_locked(
        self,
        name: str,
        arguments: str,
    ) -> dict[str, Any]:
        prospective, refreshed = self._catalog.prepare_reload(
            name,
            existing_tool_names=frozenset(self._registry.non_skill_tool_names()),
            reserved_command_names=self._reserved_command_names,
        )
        self.replace_catalog(prospective)
        if refreshed.execution_mode == "isolated":
            if self._isolated_runner is None:
                raise SkillConfigError(
                    "skill_isolated_failed",
                    "隔离 Skill 执行器尚未初始化",
                )
            result = await self._isolated_runner(refreshed, arguments)
            return {
                "name": refreshed.name,
                "execution_mode": "isolated",
                "result": result,
            }

        self._active[name] = ActiveSkill(refreshed, arguments, False)
        self._sync_controls()
        return {
            "name": refreshed.name,
            "execution_mode": "shared",
            "active": True,
        }

    def fork(self, prompt_runtime: PromptRuntime) -> SkillRuntime:
        forked = SkillRuntime(
            SkillCatalog(self._catalog.snapshot),
            self._registry,
            prompt_runtime,
            reserved_command_names=self._reserved_command_names,
            install_tools=False,
        )
        if self._isolated_runner is not None:
            forked.set_isolated_runner(self._isolated_runner)
        return forked

    def fork_current(self, prompt_runtime: PromptRuntime) -> SkillRuntime:
        """Clone active shared Skill state for an isolated worker."""

        forked = SkillRuntime(
            SkillCatalog(self._catalog.snapshot),
            self._registry,
            prompt_runtime,
            reserved_command_names=self._reserved_command_names,
            install_tools=False,
        )
        forked._active = OrderedDict(self._active)
        forked._sync_controls()
        return forked

    def prime_isolated(
        self,
        definition: SkillDefinition,
        arguments: str,
    ) -> None:
        if definition.execution_mode != "isolated":
            raise ValueError("definition 必须是 isolated Skill")
        if not isinstance(arguments, str) or "\x00" in arguments:
            raise ValueError("arguments 无效")
        self._active[definition.name] = ActiveSkill(
            definition,
            arguments,
            True,
        )
        self._sync_controls()

    def replace_catalog(self, snapshot: SkillCatalogSnapshot) -> None:
        if not isinstance(snapshot, SkillCatalogSnapshot):
            raise ValueError("snapshot 类型无效")
        tools = build_skill_script_tools(snapshot.definitions)
        definitions = {item.name: item for item in snapshot.definitions}
        replacement_active: OrderedDict[str, ActiveSkill] = OrderedDict()
        for name, active in self._active.items():
            replacement = definitions.get(name)
            if replacement is not None and (
                replacement.execution_mode == "shared"
                or active.isolated_root
            ):
                replacement_active[name] = ActiveSkill(
                    replacement,
                    active.arguments,
                    active.isolated_root,
                )
        self._registry.replace_skill_tools(tools)
        self._catalog.replace(snapshot)
        self._active = replacement_active
        self._sync_controls()

    def _install_snapshot(self, snapshot: SkillCatalogSnapshot) -> None:
        self._registry.replace_skill_tools(
            build_skill_script_tools(snapshot.definitions)
        )

    def _sync_controls(self) -> None:
        controls: list[RuntimeInstruction] = [self._catalog_control()]
        controls.extend(
            RuntimeInstruction(
                _active_instruction_id(name),
                "context",
                "session",
                (
                    f"Skill: {name}\n"
                    "Arguments:\n"
                    f"{active.arguments}\n"
                    "SOP:\n"
                    f"{active.definition.body}"
                ),
                "skill",
            )
            for name, active in self._active.items()
        )
        self._prompt_runtime.replace_dynamic_session_controls(tuple(controls))

    def _catalog_control(self) -> RuntimeInstruction:
        data = {
            "instruction": (
                "需要使用某个 Skill 时必须调用 load_skill；"
                "只能使用目录中的精确 name，不得根据 description 编造 SOP。"
            ),
            "skills": [
                {
                    "name": definition.name,
                    "description": definition.description,
                }
                for definition in self._catalog.snapshot.definitions
            ],
        }
        return RuntimeInstruction(
            "runtime.skills.catalog",
            "context",
            "session",
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            "skill",
        )
