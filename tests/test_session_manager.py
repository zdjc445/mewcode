from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage
from mewcode_agent.sessions import (
    SessionError,
    SessionJournal,
    SessionManager,
    SessionMeta,
)

ACTIVE_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TARGET_ID = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SECOND_ID = "cccccccccccccccccccccccccccccccc"


def fixed_now() -> datetime:
    return datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def create_session(
    *,
    sessions_root: Path,
    session_id: str,
    project_root: Path,
    content: str,
    now: datetime,
) -> SessionJournal:
    journal = SessionJournal(
        sessions_root=sessions_root,
        session_id=session_id,
        project_root=project_root,
        provider_id="provider",
        model="model",
        now_factory=lambda: now,
    )
    journal.append(ChatMessage(role="user", content=content))
    return journal


def test_manager_starts_lazy_without_creating_or_cleaning_sessions(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    existing = sessions_root / TARGET_ID
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    history = ConversationHistory()

    manager = SessionManager(
        sessions_root=sessions_root,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=history,
        id_factory=lambda: ACTIVE_ID,
        now_factory=fixed_now,
    )

    assert manager.active_session_id == ACTIVE_ID
    assert not (sessions_root / ACTIVE_ID).exists()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_list_uses_only_valid_meta_for_exact_project_and_sort_order(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    first_time = fixed_now() - timedelta(days=1)
    same_time = fixed_now()
    create_session(
        sessions_root=sessions_root,
        session_id=TARGET_ID,
        project_root=tmp_path,
        content="target",
        now=same_time,
    ).close()
    create_session(
        sessions_root=sessions_root,
        session_id=SECOND_ID,
        project_root=tmp_path,
        content="second",
        now=same_time,
    ).close()
    create_session(
        sessions_root=sessions_root,
        session_id="dddddddddddddddddddddddddddddddd",
        project_root=tmp_path,
        content="older",
        now=first_time,
    ).close()
    other_project = tmp_path / "other"
    other_project.mkdir()
    create_session(
        sessions_root=sessions_root,
        session_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        project_root=other_project,
        content="other",
        now=fixed_now(),
    ).close()
    invalid = sessions_root / "ffffffffffffffffffffffffffffffff"
    invalid.mkdir()
    (invalid / "meta.json").write_text("invalid", encoding="utf-8")
    (sessions_root / TARGET_ID / "messages.jsonl").unlink()
    manager = SessionManager(
        sessions_root=sessions_root,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=ConversationHistory(),
        id_factory=lambda: ACTIVE_ID,
        now_factory=fixed_now,
    )

    metas = manager.list_sessions()

    assert [item.session_id for item in metas] == [
        TARGET_ID,
        SECOND_ID,
        "dddddddddddddddddddddddddddddddd",
    ]
    assert [item.title for item in metas] == ["target", "second", "older"]


def test_resume_switches_history_and_journal_then_continues_sequence(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    target = create_session(
        sessions_root=sessions_root,
        session_id=TARGET_ID,
        project_root=tmp_path,
        content="target message",
        now=fixed_now(),
    )
    target.close()
    history = ConversationHistory()
    manager = SessionManager(
        sessions_root=sessions_root,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=history,
        id_factory=lambda: ACTIVE_ID,
        now_factory=fixed_now,
    )
    history.add_user("current message")
    current_payload = (
        sessions_root / ACTIVE_ID / "messages.jsonl"
    ).read_bytes()

    recovery = manager.resume(TARGET_ID)
    history.add_assistant("continued")

    assert manager.active_session_id == TARGET_ID
    assert recovery.messages == (
        ChatMessage(role="user", content="target message"),
    )
    assert history.snapshot()[-1] == ChatMessage(
        role="assistant",
        content="continued",
    )
    target_lines = (
        sessions_root / TARGET_ID / "messages.jsonl"
    ).read_bytes().splitlines()
    assert [json.loads(line)["sequence"] for line in target_lines] == [1, 2]
    assert (
        sessions_root / ACTIVE_ID / "messages.jsonl"
    ).read_bytes() == current_payload


def test_activation_failure_rolls_back_history_and_active_journal(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    create_session(
        sessions_root=sessions_root,
        session_id=TARGET_ID,
        project_root=tmp_path,
        content="target",
        now=fixed_now(),
    ).close()
    history = ConversationHistory()
    manager = SessionManager(
        sessions_root=sessions_root,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=history,
        id_factory=lambda: ACTIVE_ID,
        now_factory=fixed_now,
    )
    history.add_user("current")

    with pytest.raises(SessionError) as captured:
        manager.resume(
            TARGET_ID,
            activate=lambda _recovery: (_ for _ in ()).throw(
                RuntimeError("SECRET_ACTIVATION")
            ),
        )

    assert captured.value.code == "session_resume_failed"
    assert "SECRET_ACTIVATION" not in str(captured.value)
    assert manager.active_session_id == ACTIVE_ID
    assert history.snapshot() == [ChatMessage(role="user", content="current")]
    history.add_assistant("still writable")
    assert len(history) == 2


def test_start_new_preserves_old_session_and_opens_lazy_empty_history(
    tmp_path: Path,
) -> None:
    generated_ids = iter((ACTIVE_ID, SECOND_ID))
    sessions_root = tmp_path / "sessions"
    history = ConversationHistory()
    manager = SessionManager(
        sessions_root=sessions_root,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=history,
        id_factory=lambda: next(generated_ids),
        now_factory=fixed_now,
    )
    history.add_user("preserved")
    old_directory = sessions_root / ACTIVE_ID
    messages_before = (old_directory / "messages.jsonl").read_bytes()
    meta_before = (old_directory / "meta.json").read_bytes()
    activations: list[str] = []

    session_id = manager.start_new(
        activate=lambda: activations.append("activated")
    )

    assert session_id == SECOND_ID
    assert manager.active_session_id == SECOND_ID
    assert history.snapshot() == []
    assert not (sessions_root / SECOND_ID).exists()
    assert (old_directory / "messages.jsonl").read_bytes() == messages_before
    assert (old_directory / "meta.json").read_bytes() == meta_before
    assert activations == ["activated"]


def test_start_new_activation_failure_rolls_back_without_deleting_history(
    tmp_path: Path,
) -> None:
    generated_ids = iter((ACTIVE_ID, SECOND_ID))
    sessions_root = tmp_path / "sessions"
    history = ConversationHistory()
    manager = SessionManager(
        sessions_root=sessions_root,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=history,
        id_factory=lambda: next(generated_ids),
        now_factory=fixed_now,
    )
    history.add_user("preserved")

    with pytest.raises(SessionError) as captured:
        manager.start_new(
            activate=lambda: (_ for _ in ()).throw(
                RuntimeError("SECRET_ACTIVATION")
            )
        )

    assert captured.value.code == "session_switch_failed"
    assert "SECRET_ACTIVATION" not in str(captured.value)
    assert manager.active_session_id == ACTIVE_ID
    assert history.snapshot() == [ChatMessage(role="user", content="preserved")]
    history.add_assistant("continued")
    assert len(history) == 2
    assert not (sessions_root / SECOND_ID).exists()


def test_resume_gap_is_injected_at_exact_seven_day_boundary(
    tmp_path: Path,
) -> None:
    manager = SessionManager(
        sessions_root=tmp_path / "sessions",
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=ConversationHistory(),
        id_factory=lambda: ACTIVE_ID,
        now_factory=fixed_now,
    )
    at_boundary = SessionMeta(
        TARGET_ID,
        str(tmp_path.resolve()),
        "provider",
        "model",
        "title",
        "",
        1,
        1,
        (fixed_now() - timedelta(days=7)).isoformat(),
        (fixed_now() - timedelta(days=7)).isoformat(),
    )

    instruction = manager.resume_gap_instruction(at_boundary)

    assert instruction is not None
    assert instruction.instruction_id == "runtime.session.resume_gap"
    assert instruction.kind == "context"
    assert instruction.scope == "session"
    assert "完整天数=7" in instruction.content
    assert at_boundary.updated_at in instruction.content
    assert fixed_now().isoformat() in instruction.content


def test_resume_gap_is_absent_below_seven_days(tmp_path: Path) -> None:
    manager = SessionManager(
        sessions_root=tmp_path / "sessions",
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=ConversationHistory(),
        id_factory=lambda: ACTIVE_ID,
        now_factory=fixed_now,
    )
    meta = SessionMeta(
        TARGET_ID,
        str(tmp_path.resolve()),
        "provider",
        "model",
        "title",
        "",
        1,
        1,
        (fixed_now() - timedelta(days=7) + timedelta(seconds=1)).isoformat(),
        (fixed_now() - timedelta(days=7) + timedelta(seconds=1)).isoformat(),
    )

    assert manager.resume_gap_instruction(meta) is None


def test_delete_requires_explicit_target_and_rejects_active_session(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    create_session(
        sessions_root=sessions_root,
        session_id=TARGET_ID,
        project_root=tmp_path,
        content="delete me",
        now=fixed_now(),
    ).close()
    history = ConversationHistory()
    manager = SessionManager(
        sessions_root=sessions_root,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=history,
        id_factory=lambda: ACTIVE_ID,
        now_factory=fixed_now,
    )

    with pytest.raises(SessionError) as captured:
        manager.prepare_delete(ACTIVE_ID)
    assert captured.value.code == "session_delete_active"

    target = manager.prepare_delete(TARGET_ID)
    assert target.title == "delete me"
    assert target.path.exists()
    manager.delete(target)
    assert not target.path.exists()


def test_session_path_rejects_unknown_or_cross_project_target(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    other = tmp_path / "other"
    other.mkdir()
    create_session(
        sessions_root=sessions_root,
        session_id=TARGET_ID,
        project_root=other,
        content="other",
        now=fixed_now(),
    ).close()
    manager = SessionManager(
        sessions_root=sessions_root,
        project_root=tmp_path,
        provider_id="provider",
        model="model",
        history=ConversationHistory(),
        id_factory=lambda: ACTIVE_ID,
        now_factory=fixed_now,
    )

    with pytest.raises(SessionError) as cross_project:
        manager.session_path(TARGET_ID)
    with pytest.raises(SessionError) as unknown:
        manager.session_path(SECOND_ID)

    assert cross_project.value.code == "session_access_denied"
    assert unknown.value.code == "session_not_found"
