"""Public centralized command API."""

from mewcode_agent.commands.controller import CommandController
from mewcode_agent.commands.models import (
    COMMAND_CATEGORIES,
    CommandCategory,
    CommandDispatchResult,
    CommandError,
    CommandErrorCode,
    CommandExecutionKind,
    CommandHandler,
    CommandInvocation,
    CommandMode,
    CommandRegistrationError,
    CommandSpec,
    CommandUI,
    CommandUsageError,
    ConfirmationRequest,
    ParsedCommandLine,
)
from mewcode_agent.commands.parser import parse_command_line
from mewcode_agent.commands.registry import CommandRegistry

__all__ = [
    "COMMAND_CATEGORIES",
    "CommandCategory",
    "CommandController",
    "CommandDispatchResult",
    "CommandError",
    "CommandErrorCode",
    "CommandExecutionKind",
    "CommandHandler",
    "CommandInvocation",
    "CommandMode",
    "CommandRegistrationError",
    "CommandRegistry",
    "CommandSpec",
    "CommandUI",
    "CommandUsageError",
    "ConfirmationRequest",
    "ParsedCommandLine",
    "parse_command_line",
]
