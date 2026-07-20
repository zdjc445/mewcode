"""UI-independent dispatch and sanitized command error boundaries."""

from __future__ import annotations

from mewcode_agent.commands.models import (
    CommandDispatchResult,
    CommandError,
    CommandInvocation,
    CommandUI,
    CommandUsageError,
)
from mewcode_agent.commands.parser import parse_command_line
from mewcode_agent.commands.registry import CommandRegistry


class CommandController:
    def __init__(self, registry: CommandRegistry, ui: CommandUI) -> None:
        if not isinstance(registry, CommandRegistry):
            raise ValueError("registry 类型无效")
        self._registry = registry
        self._ui = ui

    @property
    def registry(self) -> CommandRegistry:
        return self._registry

    async def dispatch(self, value: str) -> CommandDispatchResult:
        parsed = parse_command_line(value)
        if not parsed.is_command:
            return CommandDispatchResult(False, None, None, None)
        if not parsed.valid_name:
            await self._ui.show_system_message(
                ("未知命令。输入 /help 查看可用命令。",)
            )
            return CommandDispatchResult(True, None, None, False)

        assert parsed.name is not None
        spec = self._registry.resolve(parsed.name)
        if spec is None:
            await self._ui.show_system_message(
                (
                    f"未知命令：/{parsed.name}。"
                    "输入 /help 查看可用命令。",
                )
            )
            return CommandDispatchResult(
                True,
                parsed.name,
                None,
                False,
            )

        invocation = CommandInvocation(
            spec,
            parsed.name,
            parsed.arguments,
        )
        try:
            await spec.handler(invocation, self._ui)
        except CommandUsageError:
            await self._ui.show_system_message(
                (
                    "命令参数无效"
                    f"（command_usage_invalid）。用法：{spec.usage}",
                )
            )
            success = False
        except CommandError as exc:
            await self._ui.show_system_message(
                (f"{exc.message}（{exc.code}）",)
            )
            success = False
        except Exception:
            await self._ui.show_system_message(
                ("命令执行失败（command_failed）",)
            )
            success = False
        else:
            success = True
        return CommandDispatchResult(
            True,
            spec.name,
            spec.execution_kind,
            success,
        )

