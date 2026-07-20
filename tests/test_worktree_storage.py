from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from mewcode_agent.worktrees import (
    WorktreeError,
    WorktreeRecord,
    WorktreeState,
    load_worktree_state,
    managed_worktree_path,
    worktree_branch_name,
    worktree_state_lock,
    write_worktree_state,
)
from mewcode_agent.worktrees import storage


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
HEAD = "1" * 40


def _record(managed_root: Path, name: str = "feature/cache") -> WorktreeRecord:
    return WorktreeRecord(
        name=name,
        path=managed_worktree_path(managed_root, name),
        branch=worktree_branch_name(name),
        base_head=HEAD,
        kind="manual",
        owner_id=None,
        created_at=NOW.isoformat(),
        last_used_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(hours=72)).isoformat(),
    )


def test_state_round_trip_is_atomic_and_sorted(tmp_path: Path) -> None:
    main_root = (tmp_path / "repo").resolve()
    main_root.mkdir()
    managed_root = main_root / ".mewcode" / "worktrees"
    state_path = tmp_path / "git" / "mewcode-agent" / "worktrees.json"
    state = WorktreeState(main_root, None, (_record(managed_root),))

    write_worktree_state(state_path, state)

    assert load_worktree_state(
        state_path,
        main_root=main_root,
        managed_root=managed_root,
    ) == state
    assert list(state_path.parent.glob("*.tmp")) == []


def test_missing_state_returns_empty_state(tmp_path: Path) -> None:
    main_root = (tmp_path / "repo").resolve()
    main_root.mkdir()

    state = load_worktree_state(
        tmp_path / "missing.json",
        main_root=main_root,
        managed_root=main_root / ".mewcode" / "worktrees",
    )

    assert state == WorktreeState(main_root, None, ())


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "path"])
def test_rejects_nonexact_or_mismatched_state(
    tmp_path: Path,
    mutation: str,
) -> None:
    main_root = (tmp_path / "repo").resolve()
    main_root.mkdir()
    managed_root = main_root / ".mewcode" / "worktrees"
    path = tmp_path / "worktrees.json"
    state = WorktreeState(main_root, None, (_record(managed_root),))
    write_worktree_state(path, state)
    text = path.read_text(encoding="utf-8")
    if mutation == "duplicate":
        text = text.replace('"version":1', '"version":1,"version":1', 1)
    else:
        data = json.loads(text)
        if mutation == "unknown":
            data["unknown"] = True
        else:
            data["records"][0]["path"] = str(tmp_path / "outside")
        text = json.dumps(data)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(WorktreeError) as caught:
        load_worktree_state(
            path,
            main_root=main_root,
            managed_root=managed_root,
        )

    assert caught.value.code == "worktree_state_invalid"


def test_lock_rejects_live_owner(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "worktrees.lock"
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

    with pytest.raises(WorktreeError) as caught:
        with worktree_state_lock(path, now=lambda: NOW):
            pass

    assert caught.value.code == "worktree_state_locked"
    assert path.exists()


def test_lock_reclaims_only_stale_dead_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "worktrees.lock"
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

    with worktree_state_lock(path, now=lambda: NOW):
        assert path.exists()

    assert not path.exists()


def test_lock_pid_unknown_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "worktrees.lock"
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

    with pytest.raises(WorktreeError) as caught:
        with worktree_state_lock(path, now=lambda: NOW):
            pass

    assert caught.value.code == "worktree_state_locked"
