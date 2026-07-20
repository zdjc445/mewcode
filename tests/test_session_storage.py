from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall
from mewcode_agent.sessions import (
    SessionError,
    SessionJournal,
    SessionMeta,
    SessionRecord,
    load_session_meta,
    recover_session,
)
from mewcode_agent.sessions import storage as session_storage
from mewcode_agent.tools.base import ToolResult

SESSION_ID = "0123456789abcdef0123456789abcdef"
CREATED_AT = "2026-07-20T10:00:00+08:00"


def fixed_now() -> datetime:
    return datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


def record_bytes(
    sequence: int,
    message: ChatMessage,
    *,
    newline: bool = True,
) -> bytes:
    payload = json.dumps(
        SessionRecord(
            SESSION_ID,
            sequence,
            CREATED_AT,
            message,
        ).to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def make_session_directory(tmp_path: Path, payload: bytes) -> Path:
    sessions_root = tmp_path / "sessions"
    directory = sessions_root / SESSION_ID
    directory.mkdir(parents=True)
    (directory / "messages.jsonl").write_bytes(payload)
    return sessions_root


def test_journal_is_lazy_and_writes_exact_record_and_meta_order(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    journal = SessionJournal(
        sessions_root=sessions_root,
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="deepseek_openai",
        model="deepseek-chat",
        now_factory=fixed_now,
    )
    assert not sessions_root.exists()

    journal.append(ChatMessage(role="user", content="\n  标题第一行  \n第二行"))
    journal.append(ChatMessage(role="assistant", content="完成\n\t结果"))

    directory = sessions_root / SESSION_ID
    lines = (directory / "messages.jsonl").read_bytes().splitlines()
    first = json.loads(lines[0])
    assert tuple(first) == (
        "schema_version",
        "session_id",
        "sequence",
        "created_at",
        "record_type",
        "message",
    )
    assert tuple(first["message"]) == (
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "thinking_blocks",
    )
    assert first["message"]["tool_calls"] == []
    assert first["message"]["tool_call_id"] is None
    assert first["message"]["thinking_blocks"] == []
    assert lines[0] + b"\n" in (directory / "messages.jsonl").read_bytes()

    meta = load_session_meta(directory / "meta.json")
    assert meta.title == "标题第一行"
    assert meta.summary == "完成 结果"
    assert meta.message_count == 2
    assert meta.last_sequence == 2
    assert meta.created_at == "2026-07-20T10:00:00+00:00"
    assert meta.updated_at == "2026-07-20T10:00:00+00:00"


def test_journal_preserves_first_user_title_and_latest_final_summary(
    tmp_path: Path,
) -> None:
    journal = SessionJournal(
        sessions_root=tmp_path / "sessions",
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )
    call = ToolCall("call-1", "read_file", "{}")
    journal.append(ChatMessage(role="user", content="first"))
    journal.append(ChatMessage(role="assistant", content="", tool_calls=(call,)))
    journal.append(
        ChatMessage(role="tool", content="result", tool_call_id="call-1")
    )
    journal.append(ChatMessage(role="user", content="second"))
    journal.append(ChatMessage(role="assistant", content="final answer"))

    assert journal.meta is not None
    assert journal.meta.title == "first"
    assert journal.meta.summary == "final answer"


def test_history_recorder_runs_before_in_memory_append() -> None:
    recorded: list[ChatMessage] = []

    def fail(message: ChatMessage) -> None:
        recorded.append(message)
        raise SessionError("session_write_failed")

    history = ConversationHistory(fail)

    with pytest.raises(SessionError, match="session_write_failed"):
        history.add_user("not committed")

    assert recorded == [ChatMessage(role="user", content="not committed")]
    assert history.snapshot() == []


def test_replace_tool_messages_does_not_call_recorder() -> None:
    calls: list[ChatMessage] = []
    history = ConversationHistory(calls.append)
    history.add_tool_result(
        "call-1",
        ToolResult(
            "read_file",
            True,
            data={"content": "original"},
        ),
    )
    original = history.snapshot()[0]
    from hashlib import sha256
    from mewcode_agent.history import ToolMessageReplacement

    replacement = ChatMessage(
        role="tool",
        content="preview",
        tool_call_id="call-1",
    )
    history.replace_tool_messages(
        (
            ToolMessageReplacement(
                0,
                "call-1",
                sha256(original.content.encode("utf-8")).hexdigest(),
                replacement,
            ),
        )
    )

    assert calls == [original]
    assert history.snapshot() == [replacement]


def test_record_round_trips_tool_calls_and_thinking_blocks() -> None:
    message = ChatMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCall("call-1", "read_file", '{"path":"a"}'),),
        thinking_blocks=(ThinkingBlock("reason", "signature"),),
    )
    record = SessionRecord(SESSION_ID, 1, CREATED_AT, message)

    restored = SessionRecord.from_dict(
        json.loads(json.dumps(record.to_dict(), ensure_ascii=False)),
        expected_session_id=SESSION_ID,
    )

    assert restored == record


def test_record_rejects_wrong_field_order_and_duplicate_json_key(
    tmp_path: Path,
) -> None:
    record = SessionRecord(
        SESSION_ID,
        1,
        CREATED_AT,
        ChatMessage(role="user", content="hello"),
    ).to_dict()
    reordered = {"session_id": record["session_id"], **record}
    with pytest.raises(ValueError, match="字段或字段顺序"):
        SessionRecord.from_dict(reordered, expected_session_id=SESSION_ID)

    sessions_root = make_session_directory(
        tmp_path,
        b'{"schema_version":1,"schema_version":1}\n',
    )
    recovery = recover_session(
        sessions_root=sessions_root,
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )
    assert recovery.diagnostics[0].code == "session_line_invalid_json"


def test_record_too_large_does_not_create_session_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(session_storage, "SESSION_RECORD_BYTES", 200)
    journal = SessionJournal(
        sessions_root=tmp_path / "sessions",
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )

    with pytest.raises(SessionError) as captured:
        journal.append(ChatMessage(role="user", content="x" * 200))

    assert captured.value.code == "session_record_too_large"
    assert not (tmp_path / "sessions").exists()


def test_meta_failure_poisoned_journal_and_keeps_history_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = SessionJournal(
        sessions_root=tmp_path / "sessions",
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )
    history = ConversationHistory(journal.append)
    monkeypatch.setattr(
        session_storage,
        "_atomic_write",
        lambda _path, _payload: (_ for _ in ()).throw(OSError("SECRET")),
    )

    with pytest.raises(SessionError) as captured:
        history.add_user("message")
    with pytest.raises(SessionError):
        history.add_user("retry")

    assert captured.value.code == "session_write_failed"
    assert "SECRET" not in str(captured.value)
    assert history.snapshot() == []


def test_recovery_skips_bad_lines_keeps_later_valid_and_normalizes_sequence(
    tmp_path: Path,
) -> None:
    payload = b"".join(
        (
            record_bytes(1, ChatMessage(role="user", content="first")),
            b"not-json\n",
            record_bytes(3, ChatMessage(role="assistant", content="later")),
        )
    )
    sessions_root = make_session_directory(tmp_path, payload)

    recovery = recover_session(
        sessions_root=sessions_root,
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )

    assert recovery.messages == (
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="later"),
    )
    assert recovery.repaired is True
    assert [item.code for item in recovery.diagnostics] == [
        "session_line_invalid_json"
    ]
    repaired_lines = (
        sessions_root / SESSION_ID / "messages.jsonl"
    ).read_bytes().splitlines(keepends=True)
    assert all(line.endswith(b"\n") for line in repaired_lines)
    assert [json.loads(line)["sequence"] for line in repaired_lines] == [1, 2]
    assert recovery.meta.message_count == 2
    assert recovery.meta.last_sequence == 2
    assert recovery.meta.title == "first"
    assert recovery.meta.summary == "later"


def test_valid_final_line_without_newline_is_recovered_and_repaired(
    tmp_path: Path,
) -> None:
    sessions_root = make_session_directory(
        tmp_path,
        record_bytes(
            1,
            ChatMessage(role="user", content="complete"),
            newline=False,
        ),
    )

    recovery = recover_session(
        sessions_root=sessions_root,
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )

    assert recovery.messages == (ChatMessage(role="user", content="complete"),)
    assert [item.code for item in recovery.diagnostics] == [
        "session_line_missing_newline"
    ]
    assert (
        sessions_root / SESSION_ID / "messages.jsonl"
    ).read_bytes().endswith(b"\n")


def test_crlf_line_is_normalized_to_single_lf(tmp_path: Path) -> None:
    sessions_root = make_session_directory(
        tmp_path,
        record_bytes(1, ChatMessage(role="user", content="complete")).replace(
            b"\n",
            b"\r\n",
        ),
    )

    recovery = recover_session(
        sessions_root=sessions_root,
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )

    assert recovery.diagnostics[0].code == "session_line_invalid_newline"
    repaired = (sessions_root / SESSION_ID / "messages.jsonl").read_bytes()
    assert repaired.endswith(b"\n") and not repaired.endswith(b"\r\n")


def test_recovery_skips_bounded_oversized_line_and_continues(
    tmp_path: Path,
    monkeypatch,
) -> None:
    limit = 500
    valid = record_bytes(2, ChatMessage(role="user", content="later"))
    assert len(valid) < limit
    sessions_root = make_session_directory(
        tmp_path,
        b"x" * (limit + 20) + b"\n" + valid,
    )
    monkeypatch.setattr(session_storage, "SESSION_RECORD_BYTES", limit)

    recovery = recover_session(
        sessions_root=sessions_root,
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )

    assert recovery.messages == (ChatMessage(role="user", content="later"),)
    assert recovery.diagnostics[0].code == "session_line_too_large"
    repaired = (sessions_root / SESSION_ID / "messages.jsonl").read_bytes()
    assert json.loads(repaired)["sequence"] == 1


def test_recovery_skips_sequence_regression_but_keeps_later_record(
    tmp_path: Path,
) -> None:
    sessions_root = make_session_directory(
        tmp_path,
        b"".join(
            (
                record_bytes(2, ChatMessage(role="user", content="first")),
                record_bytes(1, ChatMessage(role="assistant", content="skip")),
                record_bytes(3, ChatMessage(role="assistant", content="keep")),
            )
        ),
    )

    recovery = recover_session(
        sessions_root=sessions_root,
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )

    assert recovery.messages == (
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="keep"),
    )
    assert "session_line_sequence_not_increasing" in {
        item.code for item in recovery.diagnostics
    }


def test_recovery_truncates_incomplete_tool_batch_and_later_messages(
    tmp_path: Path,
) -> None:
    call = ToolCall("call-1", "read_file", "{}")
    payload = b"".join(
        (
            record_bytes(1, ChatMessage(role="user", content="read")),
            record_bytes(
                2,
                ChatMessage(role="assistant", content="", tool_calls=(call,)),
            ),
            record_bytes(3, ChatMessage(role="user", content="later")),
        )
    )
    sessions_root = make_session_directory(tmp_path, payload)

    recovery = recover_session(
        sessions_root=sessions_root,
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )

    assert recovery.messages == (ChatMessage(role="user", content="read"),)
    assert recovery.diagnostics[-1].code == "session_tool_batch_invalid"
    assert recovery.diagnostics[-1].line_number == 2
    assert recovery.meta.message_count == 1


@pytest.mark.parametrize(
    ("bad_line", "expected_code"),
    [
        (b"\xff\n", "session_line_invalid_utf8"),
        (b"{}\n", "session_line_invalid_schema"),
    ],
)
def test_recovery_reports_sanitized_bad_line_diagnostic(
    tmp_path: Path,
    bad_line: bytes,
    expected_code: str,
) -> None:
    sessions_root = make_session_directory(tmp_path, bad_line)

    recovery = recover_session(
        sessions_root=sessions_root,
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )

    assert recovery.messages == ()
    assert recovery.diagnostics[0].code == expected_code
    assert not hasattr(recovery.diagnostics[0], "content")


def test_recovery_rebuilds_corrupt_meta(tmp_path: Path) -> None:
    sessions_root = make_session_directory(
        tmp_path,
        record_bytes(1, ChatMessage(role="user", content="restored")),
    )
    meta_path = sessions_root / SESSION_ID / "meta.json"
    meta_path.write_text("SECRET_INVALID_META", encoding="utf-8")

    recovery = recover_session(
        sessions_root=sessions_root,
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )

    assert recovery.repaired is True
    assert recovery.meta.title == "restored"
    assert "SECRET_INVALID_META" not in meta_path.read_text(encoding="utf-8")


def test_recovery_rejects_valid_meta_from_different_project(
    tmp_path: Path,
) -> None:
    sessions_root = make_session_directory(
        tmp_path,
        record_bytes(1, ChatMessage(role="user", content="message")),
    )
    directory = sessions_root / SESSION_ID
    meta = SessionMeta(
        SESSION_ID,
        str((tmp_path / "other-project").absolute()),
        "provider",
        "model",
        "message",
        "",
        1,
        1,
        CREATED_AT,
        CREATED_AT,
    )
    (directory / "meta.json").write_text(
        json.dumps(meta.to_dict(), separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(SessionError) as captured:
        recover_session(
            sessions_root=sessions_root,
            session_id=SESSION_ID,
            project_root=tmp_path,
            provider_id="provider",
            model="model",
            now_factory=fixed_now,
        )

    assert captured.value.code == "session_access_denied"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_session_permissions_are_private_on_posix(tmp_path: Path) -> None:
    journal = SessionJournal(
        sessions_root=tmp_path / "sessions",
        session_id=SESSION_ID,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        now_factory=fixed_now,
    )
    journal.append(ChatMessage(role="user", content="message"))
    directory = tmp_path / "sessions" / SESSION_ID

    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "messages.jsonl").stat().st_mode & 0o777 == 0o600
    assert (directory / "meta.json").stat().st_mode & 0o777 == 0o600
