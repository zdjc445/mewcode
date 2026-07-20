"""Management and dynamic slash commands for the active Skill catalog."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from mewcode_agent.commands.models import (
    CommandDomainError,
    CommandInvocation,
    CommandSpec,
    CommandUI,
    CommandUsageError,
)
from mewcode_agent.commands.registry import CommandRegistry
from mewcode_agent.skills.catalog import scan_skill_catalog
from mewcode_agent.skills.models import (
    SkillCatalogSnapshot,
    SkillConfigError,
    SkillDiagnostic,
)
from mewcode_agent.skills.runtime import SkillRuntime
from mewcode_agent.tools.registry import ToolRegistry


SkillDiagnosticHandler = Callable[[SkillDiagnostic], None]


class SkillCommandManager:
    """Build Skill commands and apply rescan as one validated transaction."""

    def __init__(
        self,
        *,
        project_root: Path,
        user_root: Path,
        builtin_root: Path,
        tool_registry: ToolRegistry,
        skill_runtime: SkillRuntime,
        reserved_command_names: Iterable[str],
        diagnostic_handler: SkillDiagnosticHandler | None = None,
    ) -> None:
        self._project_root = project_root
        self._user_root = user_root
        self._builtin_root = builtin_root
        self._tool_registry = tool_registry
        self._skill_runtime = skill_runtime
        self._reserved_command_names = frozenset(reserved_command_names)
        self._diagnostic_handler = diagnostic_handler
        self._command_registry: CommandRegistry | None = None

    def bind_registry(self, registry: CommandRegistry) -> None:
        if not isinstance(registry, CommandRegistry) or not registry.frozen:
            raise ValueError("registry 必须是冻结的 CommandRegistry")
        if self._command_registry is not None:
            raise ValueError("Skill command registry 已绑定")
        self._command_registry = registry

    def management_spec(self) -> CommandSpec:
        return CommandSpec(
            "skills",
            (),
            "查看 Skill、显示详情或重新扫描",
            "/skills [show <name>|rescan]",
            "ui",
            "workflow",
            "精确小写 show <name> 或 rescan",
            self._manage,
        )

    def dynamic_specs(
        self,
        snapshot: SkillCatalogSnapshot | None = None,
    ) -> tuple[CommandSpec, ...]:
        current = snapshot or self._skill_runtime.catalog.snapshot
        return tuple(
            self._dynamic_spec(definition.name, definition.description)
            for definition in current.definitions
        )

    def _dynamic_spec(self, name: str, description: str) -> CommandSpec:
        async def handler(
            invocation: CommandInvocation,
            ui: CommandUI,
        ) -> None:
            prompt = (
                f"请使用 Skill `{name}` 完成任务。"
                "必须先调用 `load_skill`，并严格遵循加载后的完整 SOP。\n"
                "Skill 参数（原文）：\n"
                f"{invocation.arguments}"
            )
            await ui.send_user_message(prompt, mode="execute")

        return CommandSpec(
            name,
            (),
            description,
            f"/{name} [arguments]",
            "agent",
            "workflow",
            "传给 Skill 的可选原始参数",
            handler,
        )

    async def _manage(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        arguments = invocation.arguments
        if not arguments:
            active = {
                item.definition.name
                for item in self._skill_runtime.active_skills
            }
            lines = ["已加载 Skill："]
            for definition in self._skill_runtime.catalog.snapshot.definitions:
                lines.append(
                    " | ".join(
                        (
                            definition.name,
                            definition.description,
                            definition.source,
                            definition.execution_mode,
                            "active" if definition.name in active else "inactive",
                        )
                    )
                )
            if len(lines) == 1:
                lines.append("(空)")
            await ui.show_system_message(tuple(lines))
            ui.refresh_status("已显示 Skill 目录")
            return
        if arguments == "rescan":
            await self._rescan(ui)
            return
        if arguments.startswith("show "):
            name = arguments[5:]
            if not name or " " in name or "\t" in name:
                raise CommandUsageError
            await self._show(name, ui)
            return
        raise CommandUsageError

    async def _show(self, name: str, ui: CommandUI) -> None:
        definition = self._skill_runtime.catalog.get(name)
        if definition is None:
            raise CommandDomainError("skill_not_found", "Skill 不存在")
        active = any(
            item.definition.name == name
            for item in self._skill_runtime.active_skills
        )
        await ui.show_system_message(
            (
                f"name: {definition.name}",
                f"description: {definition.description}",
                f"source: {definition.source}",
                f"source_path: {definition.source_path}",
                f"execution_mode: {definition.execution_mode}",
                f"model: {definition.model}",
                f"context_strategy: {definition.context_strategy}",
                (
                    "recent_messages: "
                    + (
                        str(definition.recent_messages)
                        if definition.recent_messages is not None
                        else "null"
                    )
                ),
                "allowed_tools: " + ", ".join(definition.allowed_tools),
                "dedicated_tools: "
                + ", ".join(
                    tool.name for tool in definition.dedicated_tools
                ),
                f"active: {'true' if active else 'false'}",
            )
        )
        ui.refresh_status(f"已显示 Skill：{name}")

    async def _rescan(self, ui: CommandUI) -> None:
        registry = self._command_registry
        if registry is None:
            raise CommandDomainError(
                "command_unavailable",
                "Skill 命令注册中心尚未绑定",
            )
        diagnostics: list[SkillDiagnostic] = []
        try:
            snapshot = scan_skill_catalog(
                project_root=self._project_root,
                user_root=self._user_root,
                builtin_root=self._builtin_root,
                existing_tool_names=(
                    self._tool_registry.non_skill_tool_names()
                ),
                reserved_command_names=self._reserved_command_names,
                diagnostic_handler=diagnostics.append,
            )
            dynamic_specs = self.dynamic_specs(snapshot)
            registry.validate_dynamic(dynamic_specs)
            self._skill_runtime.replace_catalog(snapshot)
            registry.replace_dynamic(dynamic_specs)
        except SkillConfigError as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        except (TypeError, ValueError) as exc:
            raise CommandDomainError(
                "skill_activation_failed",
                "Skill 重新扫描事务失败",
            ) from exc
        if self._diagnostic_handler is not None:
            for diagnostic in diagnostics:
                self._diagnostic_handler(diagnostic)
        await ui.show_system_message(
            (
                "Skill 重新扫描完成："
                f"loaded={len(snapshot.definitions)}，"
                f"diagnostics={len(snapshot.diagnostics)}",
            )
        )
        ui.refresh_status("Skill 重新扫描完成")
