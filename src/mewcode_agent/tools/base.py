"""Common tool contracts and structured execution results."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

ToolCategory: TypeAlias = Literal["read", "write", "command"]


class ToolExecutionError(RuntimeError):
    """A tool failure that is safe to return to the model."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The structured result returned to the conversation history."""

    tool_name: str
    success: bool
    data: Any | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.success:
            return {
                "tool_name": self.tool_name,
                "success": True,
                "data": self.data,
            }
        return {
            "tool_name": self.tool_name,
            "success": False,
            "error": {
                "code": self.error_code,
                "message": self.error_message,
                "details": self.data,
            },
        }


class Tool(ABC):
    """Uniform interface implemented by every executable tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    category: ToolCategory
    timeout_seconds: float = 30.0

    @abstractmethod
    async def execute(self, arguments: dict[str, Any]) -> Any:
        """Execute validated JSON arguments and return serializable data."""


def validate_arguments(
    arguments: dict[str, Any],
    *,
    required: dict[str, type],
    optional: dict[str, type] | None = None,
) -> None:
    """Validate the small JSON object schemas used by the built-in tools."""

    optional = optional or {}
    allowed = set(required) | set(optional)
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ToolExecutionError(
            "invalid_arguments",
            f"包含未知参数: {', '.join(unknown)}",
        )

    missing = [name for name in required if name not in arguments]
    if missing:
        raise ToolExecutionError(
            "invalid_arguments",
            f"缺少必需参数: {', '.join(missing)}",
        )

    for name, expected_type in {**required, **optional}.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if not isinstance(value, expected_type):
            raise ToolExecutionError(
                "invalid_arguments",
                f"参数 {name} 必须为 {expected_type.__name__}",
            )
