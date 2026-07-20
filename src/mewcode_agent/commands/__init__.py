"""Public centralized command API."""

from mewcode_agent.commands.controller import CommandController
from mewcode_agent.commands.builtins import (
    REVIEW_DEFAULT_PROMPT,
    REVIEW_SCOPED_PREFIX,
    REVIEW_SCOPED_SUFFIX,
    BuiltinCommandServices,
    PermissionCommandPaths,
    build_builtin_command_registry,
)
from mewcode_agent.commands.models import (
    COMMAND_CATEGORIES,
    CommandCategory,
    CommandDispatchResult,
    CommandDomainError,
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
    "BuiltinCommandServices",
    "CommandCategory",
    "CommandController",
    "CommandDispatchResult",
    "CommandDomainError",
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
    "PermissionCommandPaths",
    "ParsedCommandLine",
    "REVIEW_DEFAULT_PROMPT",
    "REVIEW_SCOPED_PREFIX",
    "REVIEW_SCOPED_SUFFIX",
    "build_builtin_command_registry",
    "parse_command_line",
]
