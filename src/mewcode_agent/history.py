"""In-memory conversation history."""

import json

from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall
from mewcode_agent.tools.base import ToolResult


class ConversationHistory:
    """Store ordered messages for the lifetime of the current process."""

    def __init__(self) -> None:
        self._messages: list[ChatMessage] = []

    def add_user(self, content: str) -> ChatMessage:
        message = ChatMessage(role="user", content=content)
        self._messages.append(message)
        return message

    def add_assistant(self, content: str) -> ChatMessage:
        message = ChatMessage(role="assistant", content=content)
        self._messages.append(message)
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
        self._messages.append(message)
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
        self._messages.append(message)
        return message

    def snapshot(self) -> list[ChatMessage]:
        """Return a shallow copy so callers cannot mutate internal state."""

        return list(self._messages)

    def __len__(self) -> int:
        return len(self._messages)
