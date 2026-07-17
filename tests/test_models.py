import pytest

from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall


def test_assistant_tool_call_accepts_thinking_blocks() -> None:
    call = ToolCall("call_1", "read_file", '{"path":"README.md"}')
    block = ThinkingBlock("先读取文件", "sig-1")

    message = ChatMessage(
        role="assistant",
        content="",
        tool_calls=(call,),
        thinking_blocks=(block,),
    )

    assert message.thinking_blocks == (block,)


@pytest.mark.parametrize("role", ["user", "assistant", "tool"])
def test_non_tool_call_messages_reject_thinking_blocks(role: str) -> None:
    kwargs: dict[str, object] = {
        "role": role,
        "content": "内容",
        "thinking_blocks": (ThinkingBlock("不能保存"),),
    }
    if role == "tool":
        kwargs["tool_call_id"] = "call_1"

    with pytest.raises(ValueError, match="thinking_blocks"):
        ChatMessage(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["", " ", "\n\t"])
def test_thinking_block_rejects_blank_text(text: str) -> None:
    with pytest.raises(ValueError, match="text 必须"):
        ThinkingBlock(text)


def test_thinking_block_rejects_non_string_signature() -> None:
    with pytest.raises(ValueError, match="signature 必须"):
        ThinkingBlock("内容", signature=1)  # type: ignore[arg-type]
