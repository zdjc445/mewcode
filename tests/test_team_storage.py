from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from mewcode_agent.teams import (
    TeamError,
    TeamMailboxMessage,
    TeamMemberRecord,
    TeamPersistentState,
    TeamRecord,
    append_mailbox_message,
    append_member_history,
    load_mailbox,
    load_member_history,
    load_team_state,
    team_state_lock,
    write_team_state,
)
from mewcode_agent.teams import storage


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
TEAM_ID = "t" + "1" * 31
HEAD = "a" * 40


def _state(root: Path) -> TeamPersistentState:
    member = TeamMemberRecord(
        member_id="2" * 32,
        name="builder",
        role="implementer",
        backend="in_process",
        state="idle",
        current_task_id=None,
        mailbox_cursor=0,
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
    )
    team = TeamRecord(
        team_id=TEAM_ID,
        name="alpha",
        state="active",
        base_head=HEAD,
        integration_worktree_name=f"team/{TEAM_ID}/integration",
        lead_mailbox_cursor=0,
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        members=(member,),
        tasks=(),
        merged_task_ids=(),
    )
    return TeamPersistentState(root, TEAM_ID, (team,))


def _message(message_id: str = "3" * 32, content: str = "hello") -> TeamMailboxMessage:
    return TeamMailboxMessage(
        message_id=message_id,
        team_id=TEAM_ID,
        sender="lead",
        recipient="builder",
        kind="message",
        created_at=NOW.isoformat(),
        content=content,
    )


def test_state_round_trip_is_atomic(tmp_path: Path) -> None:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    path = tmp_path / "git" / "mewcode-agent" / "teams.json"
    expected = _state(root)

    write_team_state(path, expected)

    assert load_team_state(path, main_root=root) == expected
    assert list(path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "main_root"])
def test_state_rejects_nonexact_data(tmp_path: Path, mutation: str) -> None:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    path = tmp_path / "teams.json"
    write_team_state(path, _state(root))
    text = path.read_text(encoding="utf-8")
    if mutation == "duplicate":
        text = text.replace('"version":1', '"version":1,"version":1', 1)
    else:
        data = json.loads(text)
        if mutation == "unknown":
            data["unknown"] = True
        else:
            data["main_root"] = str(tmp_path / "other")
        text = json.dumps(data)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(TeamError) as caught:
        load_team_state(path, main_root=root)

    assert caught.value.code == "team_state_invalid"


def test_mailbox_append_load_and_ignore_partial_final_line(tmp_path: Path) -> None:
    path = tmp_path / "mailbox.jsonl"
    append_mailbox_message(path, _message())
    with path.open("ab") as handle:
        handle.write(b'{"version":1')

    assert load_mailbox(path) == (_message(),)


def test_mailbox_rejects_corrupt_middle_line(tmp_path: Path) -> None:
    path = tmp_path / "mailbox.jsonl"
    append_mailbox_message(path, _message())
    with path.open("ab") as handle:
        handle.write(b"not-json\n")

    with pytest.raises(TeamError) as caught:
        load_mailbox(path)

    assert caught.value.code == "team_mailbox_invalid"


def test_mailbox_rejects_duplicate_id_and_utf8_line_over_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mailbox.jsonl"
    append_mailbox_message(path, _message())
    append_mailbox_message(path, _message())
    with pytest.raises(TeamError, match="message_id"):
        load_mailbox(path)

    with pytest.raises(TeamError) as caught:
        append_mailbox_message(tmp_path / "large.jsonl", _message(content="😀" * 8192))
    assert caught.value.code == "team_mailbox_invalid"


def test_member_history_is_paired_tailed_and_not_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    append_member_history(path, "task one", "result one")
    append_member_history(path, "task two", "result two")
    original = path.read_bytes()

    messages = load_member_history(path, limit=2)

    assert [(item.role, item.content) for item in messages] == [
        ("user", "task two"),
        ("assistant", "result two"),
    ]
    assert path.read_bytes() == original


def test_member_history_ignores_partial_pair_after_crash(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    append_member_history(path, "task", "result")
    with path.open("ab") as handle:
        handle.write(b'{"version":1,"role":"user","content":"orphan"}\n')
        handle.write(b'{"version":1,"role":"assistant"')

    messages = load_member_history(path, limit=40)

    assert [(item.role, item.content) for item in messages] == [
        ("user", "task"),
        ("assistant", "result"),
    ]


def test_lock_rejects_live_owner(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "teams.lock"
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "created_at": (NOW - timedelta(hours=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "_pid_alive", lambda _pid: True)

    with pytest.raises(TeamError) as caught:
        with team_state_lock(path, now=lambda: NOW):
            pass

    assert caught.value.code == "team_state_locked"


def test_lock_reclaims_only_stale_dead_owner(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "teams.lock"
    path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "created_at": (NOW - timedelta(seconds=301)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "_pid_alive", lambda _pid: False)

    with team_state_lock(path, now=lambda: NOW):
        assert path.exists()

    assert not path.exists()


def test_lock_unknown_pid_fails_closed(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "teams.lock"
    path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "created_at": (NOW - timedelta(seconds=301)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "_pid_alive", lambda _pid: None)

    with pytest.raises(TeamError) as caught:
        with team_state_lock(path, now=lambda: NOW):
            pass

    assert caught.value.code == "team_state_locked"
