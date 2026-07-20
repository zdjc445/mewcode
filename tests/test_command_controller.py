from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from mewcode_agent.commands import (
    CommandController,
    CommandError,
    CommandInvocation,
    CommandRegistry,
    CommandSpec,
    CommandUsageError,
    ConfirmationRequest,
    parse_command_line,
)


@dataclass
class FakeUI:
    messages: list[tuple[str, ...]] = field(default_factory=list)

    async def show_system_message(self, lines: tuple[str, ...]) -> None:
        self.messages.append(lines)

    async def request_confirmation(
        self,
        _request: ConfirmationRequest,
    ) -> bool:
        return False

    async def send_user_message(self, message: str, *, mode: str) -> None:
        del message, mode

    def get_default_mode(self) -> str:
        return "execute"

    def set_default_mode(self, mode: str) -> None:
        del mode

    def clear_transcript(self) -> None:
        return None

    def refresh_status(self, state: str) -> None:
        del state


def command_spec(handler: object, *, hidden: bool = False) -> CommandSpec:
    return CommandSpec(
        "help",
        ("h", "?"),
        "show help",
        "/help [command]",
        "local",
        "general",
        "command",
        handler,  # type: ignore[arg-type]
        hidden,
        False,
    )


@pytest.mark.parametrize(
    ("value", "is_command", "valid", "name", "arguments"),
    [
        ("", False, False, None, ""),
        (" plain text ", False, False, None, ""),
        (" /HeLP   Foo BAR ", True, True, "help", "Foo BAR"),
        ("/review A\tB", True, True, "review", "A\tB"),
        ("/CODE-REVIEW", True, True, "code-review", ""),
        ("/?", True, True, "?", ""),
        ("/", True, False, None, ""),
        ("/help\targument", True, False, None, ""),
        ("/bad_name", True, False, None, ""),
    ],
)
def test_parser_has_exact_prefix_name_and_argument_boundaries(
    value: str,
    is_command: bool,
    valid: bool,
    name: str | None,
    arguments: str,
) -> None:
    parsed = parse_command_line(value)

    assert parsed.is_command is is_command
    assert parsed.valid_name is valid
    assert parsed.name == name
    assert parsed.arguments == arguments


@pytest.mark.asyncio
async def test_non_command_is_not_consumed_or_dispatched() -> None:
    calls: list[CommandInvocation] = []

    async def handler(invocation: CommandInvocation, _ui: FakeUI) -> None:
        calls.append(invocation)

    registry = CommandRegistry()
    registry.register(command_spec(handler))
    ui = FakeUI()

    result = await CommandController(registry, ui).dispatch("hello")

    assert result.consumed is False
    assert result.command_name is None
    assert result.execution_kind is None
    assert result.success is None
    assert calls == []
    assert ui.messages == []


@pytest.mark.asyncio
async def test_alias_dispatches_once_with_normalized_name_and_exact_arguments() -> None:
    calls: list[CommandInvocation] = []

    async def handler(invocation: CommandInvocation, _ui: FakeUI) -> None:
        calls.append(invocation)

    registry = CommandRegistry()
    spec = command_spec(handler)
    registry.register(spec)
    ui = FakeUI()

    result = await CommandController(registry, ui).dispatch(" /H  Target X ")

    assert result.consumed is True
    assert result.command_name == "help"
    assert result.execution_kind == "local"
    assert result.success is True
    assert len(calls) == 1
    assert calls[0].spec is spec
    assert calls[0].invoked_name == "h"
    assert calls[0].arguments == "Target X"
    assert ui.messages == []


@pytest.mark.asyncio
async def test_unknown_and_invalid_slash_inputs_are_consumed_locally() -> None:
    async def handler(_invocation: object, _ui: object) -> None:
        raise AssertionError("must not run")

    registry = CommandRegistry()
    registry.register(command_spec(handler))
    ui = FakeUI()
    controller = CommandController(registry, ui)

    unknown = await controller.dispatch("/MISSING")
    invalid = await controller.dispatch("/")

    assert unknown.consumed is True
    assert unknown.command_name == "missing"
    assert unknown.execution_kind is None
    assert unknown.success is False
    assert invalid.consumed is True
    assert invalid.command_name is None
    assert invalid.success is False
    assert ui.messages == [
        ("未知命令：/missing。输入 /help 查看可用命令。",),
        ("未知命令。输入 /help 查看可用命令。",),
    ]


@pytest.mark.asyncio
async def test_usage_error_outputs_only_stable_usage() -> None:
    async def handler(_invocation: object, _ui: object) -> None:
        raise CommandUsageError

    registry = CommandRegistry()
    registry.register(command_spec(handler))
    ui = FakeUI()

    result = await CommandController(registry, ui).dispatch("/help SECRET")

    assert result.success is False
    assert ui.messages == [
        (
            "命令参数无效（command_usage_invalid）。"
            "用法：/help [command]",
        )
    ]
    assert "SECRET" not in str(ui.messages)


@pytest.mark.asyncio
async def test_stable_and_unexpected_errors_do_not_expose_exception_text() -> None:
    outcomes: list[BaseException] = [
        CommandError("command_unavailable"),
        RuntimeError("SECRET_EXCEPTION"),
    ]

    async def handler(_invocation: object, _ui: object) -> None:
        raise outcomes.pop(0)

    registry = CommandRegistry()
    registry.register(command_spec(handler))
    ui = FakeUI()
    controller = CommandController(registry, ui)

    first = await controller.dispatch("/help")
    second = await controller.dispatch("/help")

    assert first.success is False
    assert second.success is False
    assert ui.messages == [
        ("当前命令不可用（command_unavailable）",),
        ("命令执行失败（command_failed）",),
    ]
    assert "SECRET_EXCEPTION" not in str(ui.messages)


@pytest.mark.asyncio
async def test_hidden_command_remains_exactly_dispatchable() -> None:
    calls: list[str] = []

    async def handler(invocation: CommandInvocation, _ui: object) -> None:
        calls.append(invocation.invoked_name)

    registry = CommandRegistry()
    registry.register(command_spec(handler, hidden=True))
    ui = FakeUI()

    result = await CommandController(registry, ui).dispatch("/HELP")

    assert result.success is True
    assert calls == ["help"]
    assert registry.public_specs() == ()
    assert registry.completion_candidates("h") == ()
