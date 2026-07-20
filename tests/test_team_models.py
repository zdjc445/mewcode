from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mewcode_agent.models import ChatMessage
from mewcode_agent.teams import (
    TeamBackendRequest,
    TeamBackendResult,
    TeamCloseResult,
    TeamMailboxMessage,
    TeamMemberRecord,
    TeamPersistentState,
    TeamRecord,
    TeamRuntimeConfig,
    TeamTaskRecord,
    validate_member_name,
    validate_team_hex_id,
    validate_team_id,
    validate_team_name,
)


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=1)
TEAM_ID = "t" + "1" * 31
MEMBER_ID = "2" * 32
TASK_ID = "3" * 32
OTHER_TASK_ID = "4" * 32
HEAD = "a" * 40


def _member(**changes: object) -> TeamMemberRecord:
    values: dict[str, object] = {
        "member_id": MEMBER_ID,
        "name": "builder",
        "role": "implementer",
        "backend": "in_process",
        "state": "idle",
        "current_task_id": None,
        "mailbox_cursor": 0,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }
    values.update(changes)
    return TeamMemberRecord(**values)


def _task(**changes: object) -> TeamTaskRecord:
    values: dict[str, object] = {
        "task_id": TASK_ID,
        "title": "Implement feature",
        "instructions": "Implement the requested feature.",
        "status": "pending",
        "assignee": None,
        "dependencies": (),
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }
    values.update(changes)
    return TeamTaskRecord(**values)


def _team(
    *,
    members: tuple[TeamMemberRecord, ...] | None = None,
    tasks: tuple[TeamTaskRecord, ...] = (),
) -> TeamRecord:
    return TeamRecord(
        team_id=TEAM_ID,
        name="alpha",
        state="active",
        base_head=HEAD,
        integration_worktree_name=f"team/{TEAM_ID}/integration",
        lead_mailbox_cursor=0,
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        members=(_member(),) if members is None else members,
        tasks=tasks,
        merged_task_ids=(),
    )


@pytest.mark.parametrize(
    ("validator", "valid", "invalid"),
    [
        (validate_team_name, "alpha_1", "Alpha"),
        (validate_member_name, "builder-1", "lead"),
        (validate_team_id, TEAM_ID, "t" + "1" * 30),
        (validate_team_hex_id, TASK_ID, "G" * 32),
    ],
)
def test_name_and_id_validators(validator, valid: str, invalid: str) -> None:
    assert validator(valid) == valid
    with pytest.raises(ValueError):
        validator(invalid)


def test_runtime_config_rejects_bool_range_and_odd_history() -> None:
    assert TeamRuntimeConfig().member_history_messages == 40
    with pytest.raises(ValueError):
        TeamRuntimeConfig(max_teams=True)
    with pytest.raises(ValueError):
        TeamRuntimeConfig(member_timeout_seconds=29)
    with pytest.raises(ValueError):
        TeamRuntimeConfig(member_history_messages=3)


def test_pending_cancelled_task_does_not_require_started_at() -> None:
    task = _task(
        status="cancelled",
        updated_at=LATER.isoformat(),
        ended_at=LATER.isoformat(),
        error_code="team_task_cancelled",
    )

    assert task.started_at is None


def test_team_requires_blocked_pending_to_match_dependency_state() -> None:
    dependency = _task(task_id=OTHER_TASK_ID)
    dependent = _task(
        task_id=TASK_ID,
        status="blocked",
        dependencies=(OTHER_TASK_ID,),
    )
    assert _team(tasks=tuple(sorted((dependent, dependency), key=lambda item: item.task_id)))

    with pytest.raises(ValueError, match="blocked/pending"):
        _team(
            tasks=tuple(
                sorted(
                    (replace(dependent, status="pending"), dependency),
                    key=lambda item: item.task_id,
                )
            )
        )


def test_team_rejects_dependency_cycle() -> None:
    first = _task(status="blocked", dependencies=(OTHER_TASK_ID,))
    second = _task(
        task_id=OTHER_TASK_ID,
        status="blocked",
        dependencies=(TASK_ID,),
    )

    with pytest.raises(ValueError, match="环"):
        _team(tasks=(first, second))


def test_running_member_and_task_must_match_exactly() -> None:
    member = _member(state="running", current_task_id=TASK_ID)
    task = _task(
        status="running",
        assignee="builder",
        started_at=NOW.isoformat(),
    )
    assert _team(members=(member,), tasks=(task,))

    with pytest.raises(ValueError, match="running Task"):
        _team(members=(_member(),), tasks=(task,))


def test_persistent_state_active_reference_is_strict(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    team = _team()
    assert TeamPersistentState(root, TEAM_ID, (team,)).active_team_id == TEAM_ID

    with pytest.raises(ValueError):
        TeamPersistentState(root, "t" + "9" * 31, (team,))


def test_mailbox_rejects_system_recipient() -> None:
    with pytest.raises(ValueError):
        TeamMailboxMessage(
            message_id="5" * 32,
            team_id=TEAM_ID,
            sender="lead",
            recipient="system",
            kind="message",
            created_at=NOW.isoformat(),
            content="hello",
        )


def test_backend_contract_validates_identity_history_and_workspace(
    tmp_path: Path,
) -> None:
    member = _member(state="running", current_task_id=TASK_ID)
    task = _task(
        status="running",
        assignee="builder",
        started_at=NOW.isoformat(),
    )
    request = TeamBackendRequest(
        TEAM_ID,
        member,
        task,
        (),
        (ChatMessage("user", "old"), ChatMessage("assistant", "done")),
    )
    assert request.task.task_id == TASK_ID

    with pytest.raises(ValueError, match="history"):
        replace(request, history=(ChatMessage("user", "orphan"),))

    workspace = (tmp_path / "workspace").resolve()
    result = TeamBackendResult(
        "completed",
        "implemented",
        None,
        workspace,
        True,
        "completed",
        "mewcode/worker/task",
        HEAD,
    )
    assert result.workspace_path == workspace

    with pytest.raises(ValueError, match="workspace"):
        replace(result, workspace_path=None)


def test_close_result_counts_are_bounded() -> None:
    assert TeamCloseResult(2, 1, 2).persisted_episodes == 2
    with pytest.raises(ValueError):
        TeamCloseResult(1, 2, 0)
