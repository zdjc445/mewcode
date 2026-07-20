from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from mewcode_agent.compaction import (
    CompactionConfig,
    ContextArtifactStore,
    ContextCompactionError,
    ToolResultCompactor,
)
from mewcode_agent.history import ConversationHistory, ToolMessageReplacement
from mewcode_agent.models import ChatMessage, ToolCall
from mewcode_agent.tools.base import ToolResult


SESSION_ID = "d" * 32


def config_for(
    *,
    single: int,
    batch: int,
    preview: int = 64,
) -> CompactionConfig:
    return CompactionConfig(
        single_tool_result_bytes=single,
        tool_batch_bytes=batch,
        preview_bytes=preview,
        preview_head_bytes=preview * 3 // 4,
        preview_tail_bytes=preview - (preview * 3 // 4),
        artifact_bytes=1024 * 1024,
        artifact_session_bytes=2 * 1024 * 1024,
    )


def add_tool_batch(
    history: ConversationHistory,
    results: tuple[ToolResult, ...],
) -> tuple[str, ...]:
    calls = tuple(
        ToolCall(f"call_{index}", result.tool_name, "{}")
        for index, result in enumerate(results, start=1)
    )
    history.add_assistant_tool_calls("", calls)
    original_contents: list[str] = []
    for call, result in zip(calls, results, strict=True):
        message = history.add_tool_result(call.call_id, result)
        original_contents.append(message.content)
    return tuple(original_contents)


@pytest.mark.asyncio
async def test_large_tool_result_is_written_and_replaced_with_preview(
    tmp_path: Path,
) -> None:
    config = config_for(single=180, batch=2000, preview=48)
    store = ContextArtifactStore(
        root=tmp_path / "artifacts",
        session_id=SESSION_ID,
        config=config,
    )
    history = ConversationHistory()
    original = add_tool_batch(
        history,
        (ToolResult("read_file", True, {"content": "第一段" * 100}),),
    )[0]

    result = await ToolResultCompactor(store, config=config).compact(history)
    compacted = json.loads(history.snapshot()[1].content)
    externalized = compacted["data"]["externalized"]
    restored = await store.read(
        externalized["path"],
        offset=0,
        limit=len(original),
    )

    assert result.processed_batches == 1
    assert result.externalized_results == 1
    assert compacted["tool_name"] == "read_file"
    assert compacted["success"] is True
    assert externalized["reader_tool"] == "read_context_artifact"
    assert externalized["sha256"] == sha256(original.encode("utf-8")).hexdigest()
    assert "\ufffd" not in externalized["preview"]
    assert restored["content"] == original

    await store.close()


@pytest.mark.asyncio
async def test_batch_limit_externalizes_largest_result_first(
    tmp_path: Path,
) -> None:
    history = ConversationHistory()
    originals = add_tool_batch(
        history,
        (
            ToolResult("first", True, {"content": "a" * 2000}),
            ToolResult("second", True, {"content": "b" * 1200}),
        ),
    )
    sizes = tuple(len(content.encode("utf-8")) for content in originals)
    config = config_for(
        single=max(sizes) + 1,
        batch=sum(sizes) - 1,
        preview=48,
    )
    store = ContextArtifactStore(
        root=tmp_path / "artifacts",
        session_id=SESSION_ID,
        config=config,
    )

    result = await ToolResultCompactor(store, config=config).compact(history)
    messages = history.snapshot()

    assert result.externalized_results == 1
    assert "externalized" in messages[1].content
    assert messages[2].content == originals[1]

    await store.close()


@pytest.mark.asyncio
async def test_failed_tool_result_keeps_error_code_and_message(
    tmp_path: Path,
) -> None:
    config = config_for(single=160, batch=2000, preview=48)
    store = ContextArtifactStore(
        root=tmp_path / "artifacts",
        session_id=SESSION_ID,
        config=config,
    )
    history = ConversationHistory()
    add_tool_batch(
        history,
        (
            ToolResult(
                "run_command",
                False,
                data={"stderr": "x" * 300},
                error_code="command_failed",
                error_message="命令失败",
            ),
        ),
    )

    await ToolResultCompactor(store, config=config).compact(history)
    compacted = json.loads(history.snapshot()[1].content)

    assert compacted["success"] is False
    assert compacted["error"]["code"] == "command_failed"
    assert compacted["error"]["message"] == "命令失败"
    assert "externalized" in compacted["error"]["details"]

    await store.close()


@pytest.mark.asyncio
async def test_compactor_processes_a_batch_only_once(tmp_path: Path) -> None:
    config = config_for(single=180, batch=2000, preview=48)
    store = ContextArtifactStore(
        root=tmp_path / "artifacts",
        session_id=SESSION_ID,
        config=config,
    )
    history = ConversationHistory()
    add_tool_batch(
        history,
        (ToolResult("read_file", True, {"content": "x" * 300}),),
    )
    compactor = ToolResultCompactor(store, config=config)

    first = await compactor.compact(history)
    second = await compactor.compact(history)

    assert first.processed_batches == 1
    assert second.processed_batches == 0

    await store.close()


@pytest.mark.asyncio
async def test_compactor_rejects_incomplete_or_misordered_tool_batch(
    tmp_path: Path,
) -> None:
    config = config_for(single=180, batch=2000, preview=48)
    store = ContextArtifactStore(
        root=tmp_path / "artifacts",
        session_id=SESSION_ID,
        config=config,
    )
    history = ConversationHistory()
    history.add_assistant_tool_calls(
        "",
        (
            ToolCall("first", "read_file", "{}"),
            ToolCall("second", "read_file", "{}"),
        ),
    )
    history.add_tool_result("second", ToolResult("read_file", True, {}))
    history.add_tool_result("first", ToolResult("read_file", True, {}))

    with pytest.raises(ContextCompactionError) as caught:
        await ToolResultCompactor(store, config=config).compact(history)
    assert caught.value.code == "context_invalid_tool_batch"

    await store.close()


def test_history_tool_replacement_is_atomic() -> None:
    history = ConversationHistory()
    originals = add_tool_batch(
        history,
        (
            ToolResult("first", True, {"value": 1}),
            ToolResult("second", True, {"value": 2}),
        ),
    )
    before = history.snapshot()
    first_replacement = ChatMessage(
        role="tool",
        content='{"compacted":1}',
        tool_call_id="call_1",
    )
    second_replacement = ChatMessage(
        role="tool",
        content='{"compacted":2}',
        tool_call_id="call_2",
    )

    with pytest.raises(ValueError, match="前置条件"):
        history.replace_tool_messages(
            (
                ToolMessageReplacement(
                    1,
                    "call_1",
                    sha256(originals[0].encode("utf-8")).hexdigest(),
                    first_replacement,
                ),
                ToolMessageReplacement(
                    2,
                    "call_2",
                    "0" * 64,
                    second_replacement,
                ),
            )
        )

    assert history.snapshot() == before
