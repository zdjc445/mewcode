"""In-memory conversation history."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import TYPE_CHECKING

from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall

if TYPE_CHECKING:
    from mewcode_agent.tools.base import ToolResult


@dataclass(frozen=True, slots=True)
class ToolMessageReplacement:
    index: int
    expected_tool_call_id: str
    expected_content_sha256: str
    message: ChatMessage

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("replacement index 必须是非负整数")
        if not self.expected_tool_call_id:
            raise ValueError("expected_tool_call_id 必须是非空字符串")
        if len(self.expected_content_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.expected_content_sha256
        ):
            raise ValueError("expected_content_sha256 格式无效")
        if self.message.role != "tool":
            raise ValueError("replacement message 必须是 tool 消息")


class ConversationHistory:
    """Store ordered messages for the lifetime of the current process."""

    def __init__(
        self,
        append_recorder: Callable[[ChatMessage], None] | None = None,
    ) -> None:
        self._messages: list[ChatMessage] = []
        self._append_recorder = append_recorder

    def _append(self, message: ChatMessage) -> None:
        if self._append_recorder is not None:
            self._append_recorder(message)
        self._messages.append(message)

    def add_user(self, content: str) -> ChatMessage:
        message = ChatMessage(role="user", content=content)
        self._append(message)
        return message

    def add_assistant(self, content: str) -> ChatMessage:
        message = ChatMessage(role="assistant", content=content)
        self._append(message)
        return message

    def add_assistant_tool_call(
        self,
        content: str,
        tool_call: ToolCall,
        *,
        thinking_blocks: tuple[ThinkingBlock, ...] = (),
    ) -> ChatMessage:
        return self.add_assistant_tool_calls(
            content,
            (tool_call,),
            thinking_blocks=thinking_blocks,
        )

    def add_assistant_tool_calls(
        self,
        content: str,
        tool_calls: tuple[ToolCall, ...],
        *,
        thinking_blocks: tuple[ThinkingBlock, ...] = (),
    ) -> ChatMessage:
        if not tool_calls:
            raise ValueError("tool_calls 不能为空")
        message = ChatMessage(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            thinking_blocks=thinking_blocks,
        )
        self._append(message)
        return message

    def add_tool_result(
        self,
        call_id: str,
        result: ToolResult,
    ) -> ChatMessage:
        message = ChatMessage(
            role="tool",
            content=json.dumps(
                result.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            tool_call_id=call_id,
        )
        self._append(message)
        return message

    def snapshot(self) -> list[ChatMessage]:
        """Return a shallow copy so callers cannot mutate internal state."""

        return list(self._messages)

    def set_append_recorder(
        self,
        recorder: Callable[[ChatMessage], None] | None,
    ) -> None:
        self._append_recorder = recorder

    def restore(self, messages: tuple[ChatMessage, ...]) -> None:
        if not isinstance(messages, tuple) or any(
            not isinstance(message, ChatMessage) for message in messages
        ):
            raise ValueError("messages 必须是 ChatMessage tuple")
        self._messages = list(messages)

    def replace_tool_messages(
        self,
        replacements: tuple[ToolMessageReplacement, ...],
    ) -> None:
        """Atomically replace validated tool messages without changing length."""

        if not isinstance(replacements, tuple) or not replacements:
            raise ValueError("replacements 必须是非空 tuple")
        indexes = [replacement.index for replacement in replacements]
        if len(indexes) != len(set(indexes)):
            raise ValueError("replacement index 不能重复")

        for replacement in replacements:
            if replacement.index >= len(self._messages):
                raise ValueError("replacement index 超出历史范围")
            current = self._messages[replacement.index]
            if current.role != "tool":
                raise ValueError("只能替换 tool 历史消息")
            if current.tool_call_id != replacement.expected_tool_call_id:
                raise ValueError("tool_call_id 前置条件不匹配")
            current_digest = sha256(current.content.encode("utf-8")).hexdigest()
            if current_digest != replacement.expected_content_sha256:
                raise ValueError("tool content 前置条件不匹配")
            if replacement.message.tool_call_id != current.tool_call_id:
                raise ValueError("replacement 不能修改 tool_call_id")

        updated = list(self._messages)
        for replacement in replacements:
            updated[replacement.index] = replacement.message
        self._messages = updated

    def __len__(self) -> int:
        return len(self._messages)
