from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pytest

from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall
from mewcode_agent.notes import (
    NOTES_SYSTEM_PROMPT,
    NoteUpdater,
    NotesError,
    NotesSnapshot,
)
from mewcode_agent.notes import updater as updater_module
from mewcode_agent.providers.base import (
    ProviderError,
    ProviderRequest,
    ProviderTextDelta,
    ProviderThinkingComplete,
    ProviderThinkingDelta,
    ProviderToolCall,
    ProviderTurnEnd,
    ProviderUsage,
    ProviderUsageEvent,
    ProviderUsageResult,
)


USAGE = ProviderUsageResult(
    "available",
    ProviderUsage(10, 2, 8, 5),
    None,
)


def response_json(
    *,
    user_preferences: list[str] | None = None,
    correction_feedback: list[str] | None = None,
    project_knowledge: list[str] | None = None,
    references: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "analysis_draft": ["checked facts", "checked duplicates"],
            "notes": {
                "user_preferences": user_preferences or [],
                "correction_feedback": correction_feedback or [],
                "project_knowledge": project_knowledge or [],
                "references": references or [],
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class FakeProvider:
    provider_id = "fake"
    protocol = "openai"

    def __init__(self, events: tuple[object, ...]) -> None:
        self.events = events
        self.requests: list[ProviderRequest] = []

    def prompt_payload(self, request: ProviderRequest) -> dict[str, Any]:
        return {"messages": []}

    async def stream_chat(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[object]:
        self.requests.append(request)
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


def make_updater(provider: FakeProvider, tmp_path: Path) -> NoteUpdater:
    return NoteUpdater(
        provider,  # type: ignore[arg-type]
        project_root=tmp_path,
        timeout_seconds=1,
        now_factory=lambda: datetime(
            2026,
            7,
            20,
            12,
            0,
            tzinfo=timezone.utc,
        ),
    )


@pytest.mark.asyncio
async def test_update_uses_tool_free_request_and_parses_exact_notes(
    tmp_path: Path,
) -> None:
    response = response_json(
        user_preferences=["中文回答"],
        correction_feedback=["保留原话"],
        project_knowledge=["入口 src/main.py"],
        references=["docs/spec.md"],
    )
    provider = FakeProvider(
        (
            ProviderThinkingDelta("draft"),
            ProviderThinkingComplete(ThinkingBlock("draft complete")),
            ProviderTextDelta(response[:20]),
            ProviderTextDelta(response[20:]),
            ProviderUsageEvent(USAGE),
            ProviderTurnEnd("end_turn"),
        )
    )
    usages: list[ProviderUsageResult] = []
    updater = make_updater(provider, tmp_path)

    generation = await updater.update(
        snapshot=NotesSnapshot(),
        messages=(ChatMessage(role="user", content="remember this"),),
        history_start=0,
        on_usage=usages.append,
    )

    assert generation.snapshot == NotesSnapshot(
        ("中文回答",),
        ("保留原话",),
        ("入口 src/main.py",),
        ("docs/spec.md",),
    )
    assert generation.usage_result == USAGE
    assert generation.history_end == 1
    assert generation.included_units == 1
    assert usages == [USAGE]
    request = provider.requests[0]
    assert request.tools is None
    assert request.system_prompt == NOTES_SYSTEM_PROMPT
    assert request.system_prompt.splitlines()[0] == "禁止调用任何工具。"
    assert request.system_prompt.splitlines()[-1] == "禁止调用任何工具。"
    source_message = request.items[0]
    assert isinstance(source_message, ChatMessage)
    source = json.loads(source_message.content)
    assert tuple(source) == (
        "schema_version",
        "current_notes",
        "recent_history_units",
        "project_root",
        "current_time",
        "instructions",
    )
    assert source["project_root"] == str(tmp_path.resolve())
    assert source["current_time"] == "2026-07-20T12:00:00+00:00"


@pytest.mark.asyncio
async def test_input_uses_recent_twelve_atomic_units_after_cursor(
    tmp_path: Path,
) -> None:
    response = response_json()
    provider = FakeProvider(
        (
            ProviderTextDelta(response),
            ProviderUsageEvent(USAGE),
            ProviderTurnEnd("end_turn"),
        )
    )
    updater = make_updater(provider, tmp_path)
    messages = tuple(
        ChatMessage(role="user", content=f"unit-{index}")
        for index in range(15)
    )

    result = await updater.update(
        snapshot=NotesSnapshot(),
        messages=messages,
        history_start=1,
    )

    source_message = provider.requests[0].items[0]
    assert isinstance(source_message, ChatMessage)
    units = json.loads(source_message.content)["recent_history_units"]
    assert result.included_units == 12
    assert len(units) == 12
    assert units[0][0]["content"] == "unit-3"
    assert units[-1][0]["content"] == "unit-14"


@pytest.mark.asyncio
async def test_input_never_splits_tool_batch_when_dropping_old_units(
    tmp_path: Path,
    monkeypatch,
) -> None:
    response = response_json()
    provider = FakeProvider(
        (
            ProviderTextDelta(response),
            ProviderUsageEvent(USAGE),
            ProviderTurnEnd("end_turn"),
        )
    )
    updater = make_updater(provider, tmp_path)
    call = ToolCall("call-1", "read_file", "{}")
    messages = (
        ChatMessage(role="user", content="old" * 200),
        ChatMessage(role="assistant", content="", tool_calls=(call,)),
        ChatMessage(
            role="tool",
            content="result" * 30,
            tool_call_id="call-1",
        ),
        ChatMessage(role="assistant", content="final"),
    )
    monkeypatch.setattr(updater_module, "NOTES_INPUT_BYTES", 1000)

    await updater.update(
        snapshot=NotesSnapshot(),
        messages=messages,
        history_start=0,
    )

    source_message = provider.requests[0].items[0]
    assert isinstance(source_message, ChatMessage)
    units = json.loads(source_message.content)["recent_history_units"]
    for unit in units:
        has_tool_call = any(
            message["role"] == "assistant" and message["tool_calls"]
            for message in unit
        )
        if has_tool_call:
            assert [message["role"] for message in unit] == [
                "assistant",
                "tool",
            ]
    assert units[-1][0]["content"] == "final"


@pytest.mark.asyncio
async def test_existing_notes_are_not_truncated_to_fit_input(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = FakeProvider(())
    updater = make_updater(provider, tmp_path)
    monkeypatch.setattr(updater_module, "NOTES_INPUT_BYTES", 100)

    with pytest.raises(NotesError) as captured:
        await updater.update(
            snapshot=NotesSnapshot(user_preferences=("x" * 80,)),
            messages=(),
            history_start=0,
        )

    assert captured.value.code == "notes_update_failed"
    assert provider.requests == []


@pytest.mark.asyncio
async def test_tool_call_is_forbidden(tmp_path: Path) -> None:
    provider = FakeProvider(
        (ProviderToolCall(ToolCall("call-1", "read_file", "{}")),)
    )

    with pytest.raises(NotesError) as captured:
        await make_updater(provider, tmp_path).update(
            snapshot=NotesSnapshot(),
            messages=(),
            history_start=0,
        )

    assert captured.value.code == "notes_tool_call_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        (ProviderTextDelta(response_json()), ProviderTurnEnd("end_turn")),
        (
            ProviderTextDelta(response_json()),
            ProviderUsageEvent(USAGE),
            ProviderUsageEvent(USAGE),
            ProviderTurnEnd("end_turn"),
        ),
        (
            ProviderTextDelta(response_json()),
            ProviderUsageEvent(USAGE),
            ProviderTextDelta("late"),
            ProviderTurnEnd("end_turn"),
        ),
        (
            ProviderTextDelta(response_json()),
            ProviderUsageEvent(USAGE),
            ProviderTurnEnd("max_tokens"),
        ),
        (
            ProviderTextDelta(response_json()),
            ProviderUsageEvent(USAGE),
        ),
    ],
)
async def test_invalid_provider_event_contract_is_rejected(
    tmp_path: Path,
    events: tuple[object, ...],
) -> None:
    provider = FakeProvider(events)

    with pytest.raises(NotesError) as captured:
        await make_updater(provider, tmp_path).update(
            snapshot=NotesSnapshot(),
            messages=(),
            history_start=0,
        )

    assert captured.value.code == "notes_update_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        '{"notes":{},"analysis_draft":[]}',
        '{"analysis_draft":[],"analysis_draft":[],"notes":{}}',
        '{"analysis_draft":"wrong","notes":{}}',
        json.dumps(
            {
                "analysis_draft": [],
                "notes": {
                    "references": [],
                    "project_knowledge": [],
                    "correction_feedback": [],
                    "user_preferences": [],
                },
            },
            separators=(",", ":"),
        ),
        response_json(user_preferences=["x\ny"]),
    ],
)
async def test_invalid_json_contract_is_rejected(
    tmp_path: Path,
    content: str,
) -> None:
    provider = FakeProvider(
        (
            ProviderTextDelta(content),
            ProviderUsageEvent(USAGE),
            ProviderTurnEnd("end_turn"),
        )
    )

    with pytest.raises(NotesError) as captured:
        await make_updater(provider, tmp_path).update(
            snapshot=NotesSnapshot(),
            messages=(),
            history_start=0,
        )

    assert captured.value.code == "notes_update_invalid"


@pytest.mark.asyncio
async def test_response_size_is_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(updater_module, "NOTES_RESPONSE_BYTES", 20)
    provider = FakeProvider((ProviderTextDelta("x" * 21),))

    with pytest.raises(NotesError) as captured:
        await make_updater(provider, tmp_path).update(
            snapshot=NotesSnapshot(),
            messages=(),
            history_start=0,
        )

    assert captured.value.code == "notes_update_invalid"


@pytest.mark.asyncio
async def test_provider_failure_is_sanitized(tmp_path: Path) -> None:
    provider = FakeProvider((ProviderError("SECRET_PROVIDER"),))

    with pytest.raises(NotesError) as captured:
        await make_updater(provider, tmp_path).update(
            snapshot=NotesSnapshot(),
            messages=(),
            history_start=0,
        )

    assert captured.value.code == "notes_update_failed"
    assert "SECRET_PROVIDER" not in str(captured.value)
