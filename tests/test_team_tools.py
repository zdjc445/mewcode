from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mewcode_agent.teams import (
    TeamCreateTool,
    TeamError,
    TeamIntegrateTool,
    TeamMailboxMessage,
    TeamMemberRecord,
    TeamRecord,
    TeamStatusTool,
    TeamTaskRecord,
    TeamTaskTool,
    TeamMessageTool,
    team_tools,
)
from mewcode_agent.tools import ToolExecutionError
from mewcode_agent.worktrees import WorktreeStatus


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc).isoformat()
TEAM_ID = "t" + "1" * 31
TASK_ID = "2" * 32
HEAD = "a" * 40


def _member() -> TeamMemberRecord:
    return TeamMemberRecord(
        "3" * 32,
        "build",
        "implementer",
        "in_process",
        "idle",
        None,
        0,
        NOW,
        NOW,
    )


def _task(status: str = "pending") -> TeamTaskRecord:
    values = {
        "task_id": TASK_ID,
        "title": "Feature",
        "instructions": "Implement feature.",
        "status": status,
        "assignee": "build",
        "dependencies": (),
        "created_at": NOW,
        "updated_at": NOW,
    }
    if status == "completed":
        values.update(
            started_at=NOW,
            ended_at=NOW,
            result="done",
            workspace_path=Path.cwd().resolve(),
            workspace_preserved=True,
            workspace_reason="team_integration_pending",
            branch="branch-a",
            head=HEAD,
        )
    return TeamTaskRecord(**values)  # type: ignore[arg-type]


def _team(tasks: tuple[TeamTaskRecord, ...] = ()) -> TeamRecord:
    return TeamRecord(
        TEAM_ID,
        "alpha",
        "active",
        HEAD,
        f"team/{TEAM_ID}/integration",
        0,
        NOW,
        NOW,
        (_member(),),
        tasks,
        (),
    )


class StubManager:
    def __init__(self) -> None:
        self.team = _team()
        self.calls: list[tuple[str, object]] = []
        self.error: TeamError | None = None

    def _check(self) -> None:
        if self.error is not None:
            raise self.error

    async def create_team(self, name, members):
        self._check()
        self.calls.append(("create_team", (name, members)))
        return self.team

    async def create_task(self, title, instructions, *, assignee, depends_on):
        self._check()
        self.calls.append(
            ("create_task", (title, instructions, assignee, depends_on))
        )
        return _task()

    async def list_tasks(self):
        self._check()
        return (_task(),)

    async def get_task(self, task_id):
        self._check()
        self.calls.append(("get_task", task_id))
        return _task("completed")

    async def cancel_task(self, task_id):
        self._check()
        self.calls.append(("cancel_task", task_id))
        return _task()

    async def send_message(self, recipient, content):
        self._check()
        self.calls.append(("send_message", (recipient, content)))
        return TeamMailboxMessage(
            "4" * 32,
            TEAM_ID,
            "lead",
            recipient,
            "message",
            NOW,
            content,
        )

    async def get_team(self):
        self._check()
        return self.team

    async def integration_status(self):
        self._check()
        return WorktreeStatus(True, HEAD, False, 0, None, 0, False, True, None)

    async def integrate_task(self, task_id):
        self._check()
        self.calls.append(("integrate_task", task_id))
        return _task("completed")


async def test_fixed_team_tools_and_create_schema_are_exact() -> None:
    manager = StubManager()
    tools = team_tools(manager)  # type: ignore[arg-type]

    assert [tool.name for tool in tools] == [
        "team_create",
        "team_task",
        "team_message",
        "team_status",
        "team_integrate",
    ]
    result = await TeamCreateTool(manager).execute(  # type: ignore[arg-type]
        {
            "name": "alpha",
            "members": [{"name": "build", "role": "implementer"}],
        }
    )
    assert result["team_id"] == TEAM_ID
    assert manager.calls[0] == (
        "create_team",
        ("alpha", (("build", "implementer"),)),
    )
    with pytest.raises(ToolExecutionError) as caught:
        await TeamCreateTool(manager).execute(  # type: ignore[arg-type]
            {
                "name": "alpha",
                "members": [
                    {"name": "build", "role": "implementer", "extra": True}
                ],
            }
        )
    assert caught.value.code == "invalid_arguments"


async def test_team_task_actions_are_exact_and_detailed_get() -> None:
    manager = StubManager()
    tool = TeamTaskTool(manager)  # type: ignore[arg-type]

    created = await tool.execute(
        {
            "action": "create",
            "title": "Feature",
            "instructions": "Implement feature.",
            "assignee": None,
            "depends_on": [],
        }
    )
    listed = await tool.execute({"action": "list"})
    detailed = await tool.execute({"action": "get", "task_id": TASK_ID})

    assert created["status"] == "pending"
    assert len(listed) == 1
    assert detailed["instructions"] == "Implement feature."
    assert detailed["result"] == "done"
    with pytest.raises(ToolExecutionError) as caught:
        await tool.execute({"action": "list", "task_id": TASK_ID})
    assert caught.value.code == "invalid_arguments"


async def test_message_status_integrate_and_domain_error_mapping() -> None:
    manager = StubManager()

    message = await TeamMessageTool(manager).execute(  # type: ignore[arg-type]
        {"recipient": "build", "content": "Check cache."}
    )
    status = await TeamStatusTool(manager).execute({})  # type: ignore[arg-type]
    integrated = await TeamIntegrateTool(manager).execute(  # type: ignore[arg-type]
        {"task_id": TASK_ID}
    )

    assert message["recipient"] == "build"
    assert status["name"] == "alpha"
    assert integrated["task_id"] == TASK_ID
    manager.error = TeamError("team_paused", "Team 已暂停")
    with pytest.raises(ToolExecutionError) as caught:
        await TeamStatusTool(manager).execute({})  # type: ignore[arg-type]
    assert caught.value.code == "team_paused"
    assert caught.value.message == "Team 已暂停"
