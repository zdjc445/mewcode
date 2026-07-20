import pytest

from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall


def test_history_keeps_message_order() -> None:
    history = ConversationHistory()

    history.add_user("第一问")
    history.add_assistant("第一答")
    history.add_user("第二问")

    assert history.snapshot() == [
        ChatMessage(role="user", content="第一问"),
        ChatMessage(role="assistant", content="第一答"),
        ChatMessage(role="user", content="第二问"),
    ]
    assert len(history) == 3


def test_snapshot_does_not_mutate_internal_history() -> None:
    history = ConversationHistory()
    history.add_user("保留内容")

    snapshot = history.snapshot()
    snapshot.clear()

    assert len(history) == 1
    assert history.snapshot()[0].content == "保留内容"


def test_history_can_replace_recorder_and_restore_without_recording() -> None:
    first: list[ChatMessage] = []
    second: list[ChatMessage] = []
    history = ConversationHistory(first.append)
    history.add_user("first")
    history.set_append_recorder(second.append)
    history.restore((ChatMessage(role="assistant", content="restored"),))
    history.add_user("second")

    assert first == [ChatMessage(role="user", content="first")]
    assert second == [ChatMessage(role="user", content="second")]
    assert history.snapshot() == [
        ChatMessage(role="assistant", content="restored"),
        ChatMessage(role="user", content="second"),
    ]


@pytest.mark.parametrize("role", ["system", "User", "ASSISTANT", ""])
def test_chat_message_rejects_invalid_role(role: str) -> None:
    with pytest.raises(ValueError, match="role 必须"):
        ChatMessage(role=role, content="内容")  # type: ignore[arg-type]


@pytest.mark.parametrize("content", ["", " ", "\n\t"])
def test_chat_message_rejects_blank_content(content: str) -> None:
    with pytest.raises(ValueError, match="content 必须"):
        ChatMessage(role="user", content=content)


def test_history_keeps_thinking_on_assistant_tool_call() -> None:
    history = ConversationHistory()
    call = ToolCall("call_1", "read_file", '{"path":"README.md"}')
    block = ThinkingBlock("读取后回答", "sig-1")

    history.add_assistant_tool_calls(
        "",
        (call,),
        thinking_blocks=(block,),
    )

    assert history.snapshot() == [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=(call,),
            thinking_blocks=(block,),
        )
    ]
