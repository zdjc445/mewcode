"""Validated command metadata and UI-independent command contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import re
from typing import Literal, Protocol, TypeAlias


CommandExecutionKind: TypeAlias = Literal["local", "ui", "agent"]
CommandCategory: TypeAlias = Literal[
    "general",
    "workflow",
    "context",
    "sessions",
    "memory",
    "security",
]
CommandMode: TypeAlias = Literal["plan", "execute"]
CommandErrorCode: TypeAlias = Literal[
    "command_registry_invalid",
    "command_usage_invalid",
    "command_unavailable",
    "command_failed",
    "command_confirmation_failed",
    "command_status_failed",
    "session_switch_failed",
]

COMMAND_CATEGORIES: tuple[CommandCategory, ...] = (
    "general",
    "workflow",
    "context",
    "sessions",
    "memory",
    "security",
)
_COMMAND_NAME = re.compile(r"(?:[a-z][a-z0-9-]*|\?)\Z")
_ERROR_MESSAGES: dict[CommandErrorCode, str] = {
    "command_registry_invalid": "命令注册中心无效",
    "command_usage_invalid": "命令参数无效",
    "command_unavailable": "当前命令不可用",
    "command_failed": "命令执行失败",
    "command_confirmation_failed": "命令确认失败",
    "command_status_failed": "命令状态读取失败",
    "session_switch_failed": "会话切换失败",
}


class CommandRegistrationError(ValueError):
    """Reject invalid or conflicting registry metadata."""

    code = "command_registry_invalid"


class CommandError(Exception):
    """Stable, content-free command failure."""

    def __init__(self, code: CommandErrorCode) -> None:
        if code not in _ERROR_MESSAGES:
            raise ValueError("未知命令错误码")
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        super().__init__(self.message)


class CommandUsageError(CommandError):
    def __init__(self) -> None:
        super().__init__("command_usage_invalid")


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    action_id: str
    title: str
    fields: tuple[tuple[str, str], ...]
    destructive: bool

    def __post_init__(self) -> None:
        if not _is_nonempty_single_line(self.action_id):
            raise ValueError("action_id 必须是非空单行字符串")
        if not _is_nonempty_single_line(self.title):
            raise ValueError("title 必须是非空单行字符串")
        if not isinstance(self.fields, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not _is_nonempty_single_line(item[0])
            or not isinstance(item[1], str)
            or "\x00" in item[1]
            for item in self.fields
        ):
            raise ValueError("confirmation fields 无效")
        if type(self.destructive) is not bool:
            raise ValueError("destructive 必须是 bool")


class CommandUI(Protocol):
    async def show_system_message(self, lines: tuple[str, ...]) -> None: ...

    async def request_confirmation(
        self,
        request: ConfirmationRequest,
    ) -> bool: ...

    async def send_user_message(
        self,
        message: str,
        *,
        mode: CommandMode,
    ) -> None: ...

    def get_default_mode(self) -> CommandMode: ...

    def set_default_mode(self, mode: CommandMode) -> None: ...

    def clear_transcript(self) -> None: ...

    def refresh_status(self, state: str) -> None: ...


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    spec: CommandSpec
    invoked_name: str
    arguments: str

    def __post_init__(self) -> None:
        if not isinstance(self.spec, CommandSpec):
            raise ValueError("spec 类型无效")
        if not _COMMAND_NAME.fullmatch(self.invoked_name):
            raise ValueError("invoked_name 格式无效")
        if self.invoked_name not in (self.spec.name, *self.spec.aliases):
            raise ValueError("invoked_name 不属于命令")
        if not isinstance(self.arguments, str) or "\x00" in self.arguments:
            raise ValueError("arguments 无效")


CommandHandler: TypeAlias = Callable[
    [CommandInvocation, CommandUI], Awaitable[None]
]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    aliases: tuple[str, ...]
    description: str
    usage: str
    execution_kind: CommandExecutionKind
    category: CommandCategory
    argument_hint: str
    handler: CommandHandler = field(repr=False, compare=False)
    hidden: bool = False
    status_hint: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _COMMAND_NAME.fullmatch(
            self.name
        ):
            raise CommandRegistrationError("命令 name 格式无效")
        if not isinstance(self.aliases, tuple) or any(
            not isinstance(alias, str) or not _COMMAND_NAME.fullmatch(alias)
            for alias in self.aliases
        ):
            raise CommandRegistrationError("命令 aliases 格式无效")
        keys = (self.name, *self.aliases)
        if len(keys) != len(set(keys)):
            raise CommandRegistrationError("命令 name 或 alias 重复")
        if not _is_nonempty_single_line(self.description):
            raise CommandRegistrationError("命令 description 无效")
        if not _is_nonempty_single_line(self.usage):
            raise CommandRegistrationError("命令 usage 无效")
        usage_prefix = f"/{self.name}"
        if not (
            self.usage == usage_prefix
            or self.usage.startswith(usage_prefix + " ")
        ):
            raise CommandRegistrationError("命令 usage 未使用规范 name")
        if self.execution_kind not in ("local", "ui", "agent"):
            raise CommandRegistrationError("命令 execution_kind 无效")
        if self.category not in COMMAND_CATEGORIES:
            raise CommandRegistrationError("命令 category 无效")
        if not isinstance(self.argument_hint, str) or any(
            character in self.argument_hint for character in "\r\n\x00"
        ):
            raise CommandRegistrationError("命令 argument_hint 无效")
        if not callable(self.handler):
            raise CommandRegistrationError("命令 handler 无效")
        if type(self.hidden) is not bool or type(self.status_hint) is not bool:
            raise CommandRegistrationError("命令可见性元数据无效")
        if self.hidden and self.status_hint:
            raise CommandRegistrationError("隐藏命令不能用于状态栏提示")


@dataclass(frozen=True, slots=True)
class ParsedCommandLine:
    is_command: bool
    valid_name: bool
    name: str | None
    arguments: str

    def __post_init__(self) -> None:
        if type(self.is_command) is not bool or type(self.valid_name) is not bool:
            raise ValueError("解析状态必须是 bool")
        if not self.is_command:
            if self.valid_name or self.name is not None or self.arguments:
                raise ValueError("非命令解析结果包含命令字段")
            return
        if self.valid_name:
            if self.name is None or not _COMMAND_NAME.fullmatch(self.name):
                raise ValueError("有效命令名格式无效")
        elif self.name is not None:
            raise ValueError("无效命令名必须为 None")
        if not isinstance(self.arguments, str):
            raise ValueError("arguments 必须是字符串")


@dataclass(frozen=True, slots=True)
class CommandDispatchResult:
    consumed: bool
    command_name: str | None
    execution_kind: CommandExecutionKind | None
    success: bool | None

    def __post_init__(self) -> None:
        if type(self.consumed) is not bool:
            raise ValueError("consumed 必须是 bool")
        if not self.consumed:
            if any(
                item is not None
                for item in (
                    self.command_name,
                    self.execution_kind,
                    self.success,
                )
            ):
                raise ValueError("未消费结果不能包含命令状态")
            return
        if self.command_name is not None and not _COMMAND_NAME.fullmatch(
            self.command_name
        ):
            raise ValueError("command_name 格式无效")
        if self.execution_kind is not None and self.execution_kind not in (
            "local",
            "ui",
            "agent",
        ):
            raise ValueError("execution_kind 无效")
        if type(self.success) is not bool:
            raise ValueError("已消费结果必须包含 success")


def _is_nonempty_single_line(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(character in value for character in "\r\n\x00")
    )

