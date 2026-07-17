"""Conversation and streamed tool-call data models."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

ChatRole: TypeAlias = Literal["user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One complete tool call assembled from streamed argument fragments."""

    call_id: str
    name: str
    arguments_json: str

    def __post_init__(self) -> None:
        if not self.call_id:
            raise ValueError("call_id 必须为非空字符串")
        if not self.name:
            raise ValueError("name 必须为非空字符串")
        if not isinstance(self.arguments_json, str):
            raise ValueError("arguments_json 必须为字符串")


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One validated message in the in-memory conversation."""

    role: ChatRole
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant", "tool"):
            raise ValueError("role 必须为 user、assistant 或 tool")
        if not isinstance(self.content, str):
            raise ValueError("content 必须为字符串")
        if self.role == "assistant" and self.tool_calls:
            if self.tool_call_id is not None:
                raise ValueError("assistant 消息不能包含 tool_call_id")
            return
        if not self.content.strip():
            raise ValueError("content 必须为非空字符串")
        if self.role == "tool":
            if not self.tool_call_id:
                raise ValueError("tool 消息必须包含 tool_call_id")
            if self.tool_calls:
                raise ValueError("tool 消息不能包含 tool_calls")
        elif self.tool_calls or self.tool_call_id is not None:
            raise ValueError(f"{self.role} 消息不能包含工具字段")
