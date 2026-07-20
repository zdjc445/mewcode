"""Provider-facing tools for persistent Team collaboration."""

from __future__ import annotations

from collections import Counter
from typing import Any

from mewcode_agent.teams.manager import TeamManager
from mewcode_agent.teams.models import TeamError, TeamRecord, TeamTaskRecord
from mewcode_agent.tools.base import Tool, ToolExecutionError, validate_arguments


def _raise_tool_error(error: TeamError) -> ToolExecutionError:
    return ToolExecutionError(error.code, error.message)


def _workspace_data(task: TeamTaskRecord) -> dict[str, object] | None:
    if task.workspace_path is None:
        return None
    return {
        "path": str(task.workspace_path),
        "preserved": task.workspace_preserved,
        "reason": task.workspace_reason,
        "branch": task.branch,
        "head": task.head,
    }


def _task_summary(
    task: TeamTaskRecord,
    *,
    detailed: bool,
) -> dict[str, object]:
    result: dict[str, object] = {
        "task_id": task.task_id,
        "title": task.title,
        "status": task.status,
        "assignee": task.assignee,
        "dependencies": list(task.dependencies),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "ended_at": task.ended_at,
        "error_code": task.error_code,
        "workspace": _workspace_data(task),
        "integrated_head": task.integrated_head,
    }
    if detailed:
        result["instructions"] = task.instructions
        result["result"] = task.result
    return result


def _team_summary(team: TeamRecord) -> dict[str, object]:
    counts = Counter(task.status for task in team.tasks)
    return {
        "team_id": team.team_id,
        "name": team.name,
        "state": team.state,
        "integration_worktree": team.integration_worktree_name,
        "created_at": team.created_at,
        "updated_at": team.updated_at,
        "members": [
            {
                "member_id": member.member_id,
                "name": member.name,
                "role": member.role,
                "state": member.state,
                "current_task_id": member.current_task_id,
            }
            for member in team.members
        ],
        "task_counts": {
            status: counts.get(status, 0)
            for status in (
                "blocked",
                "pending",
                "running",
                "completed",
                "integrated",
                "failed",
                "cancelled",
            )
        },
    }


class TeamCreateTool(Tool):
    name = "team_create"
    description = "创建一个持久 Team，并为每个成员绑定 worktree 隔离角色。"
    category = "write"
    timeout_seconds = 120.0
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "members": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "role": {"type": "string"},
                    },
                    "required": ["name", "role"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["name", "members"],
        "additionalProperties": False,
    }

    def __init__(self, manager: TeamManager) -> None:
        self._manager = manager

    async def execute(self, arguments: dict[str, Any]) -> object:
        validate_arguments(arguments, required={"name": str, "members": list})
        raw_members = arguments["members"]
        assert isinstance(raw_members, list)
        members: list[tuple[str, str]] = []
        for item in raw_members:
            if (
                not isinstance(item, dict)
                or set(item) != {"name", "role"}
                or not isinstance(item["name"], str)
                or not isinstance(item["role"], str)
            ):
                raise ToolExecutionError(
                    "invalid_arguments",
                    "members 必须精确包含 name/role 字符串",
                )
            members.append((item["name"], item["role"]))
        try:
            team = await self._manager.create_team(
                arguments["name"],
                tuple(members),
            )
        except TeamError as exc:
            raise _raise_tool_error(exc) from exc
        return _team_summary(team)


class TeamTaskTool(Tool):
    name = "team_task"
    description = "创建、列出、读取或取消当前 Team 的 DAG task。"
    category = "write"
    timeout_seconds = 120.0
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "get", "cancel"],
            },
            "title": {"type": "string"},
            "instructions": {"type": "string"},
            "assignee": {"type": ["string", "null"]},
            "depends_on": {"type": "array", "items": {"type": "string"}},
            "task_id": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, manager: TeamManager) -> None:
        self._manager = manager

    async def execute(self, arguments: dict[str, Any]) -> object:
        action = arguments.get("action")
        if not isinstance(action, str):
            raise ToolExecutionError("invalid_arguments", "action 必须是字符串")
        try:
            if action == "create":
                if not {"action", "title", "instructions"}.issubset(arguments) or not set(
                    arguments
                ).issubset(
                    {"action", "title", "instructions", "assignee", "depends_on"}
                ):
                    raise ToolExecutionError(
                        "invalid_arguments",
                        "create 字段无效",
                    )
                title = arguments["title"]
                instructions = arguments["instructions"]
                assignee = arguments.get("assignee")
                dependencies = arguments.get("depends_on", [])
                if (
                    not isinstance(title, str)
                    or not isinstance(instructions, str)
                    or (assignee is not None and not isinstance(assignee, str))
                    or not isinstance(dependencies, list)
                    or any(not isinstance(item, str) for item in dependencies)
                ):
                    raise ToolExecutionError(
                        "invalid_arguments",
                        "create 参数类型无效",
                    )
                task = await self._manager.create_task(
                    title,
                    instructions,
                    assignee=assignee,
                    depends_on=tuple(dependencies),
                )
                return _task_summary(task, detailed=False)
            if action == "list" and set(arguments) == {"action"}:
                return [
                    _task_summary(task, detailed=False)
                    for task in await self._manager.list_tasks()
                ]
            if action in ("get", "cancel") and set(arguments) == {
                "action",
                "task_id",
            }:
                task_id = arguments["task_id"]
                if not isinstance(task_id, str):
                    raise ToolExecutionError(
                        "invalid_arguments",
                        "task_id 必须是字符串",
                    )
                task = (
                    await self._manager.get_task(task_id)
                    if action == "get"
                    else await self._manager.cancel_task(task_id)
                )
                return _task_summary(task, detailed=True)
        except TeamError as exc:
            raise _raise_tool_error(exc) from exc
        raise ToolExecutionError("invalid_arguments", "team_task action 或字段无效")


class TeamMessageTool(Tool):
    name = "team_message"
    description = "向当前 Team 的指定成员追加一条 Lead mailbox 消息。"
    category = "write"
    parameters = {
        "type": "object",
        "properties": {
            "recipient": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["recipient", "content"],
        "additionalProperties": False,
    }

    def __init__(self, manager: TeamManager) -> None:
        self._manager = manager

    async def execute(self, arguments: dict[str, Any]) -> object:
        validate_arguments(
            arguments,
            required={"recipient": str, "content": str},
        )
        try:
            message = await self._manager.send_message(
                arguments["recipient"],
                arguments["content"],
            )
        except TeamError as exc:
            raise _raise_tool_error(exc) from exc
        return {
            "message_id": message.message_id,
            "team_id": message.team_id,
            "recipient": message.recipient,
            "kind": message.kind,
            "created_at": message.created_at,
        }


class TeamStatusTool(Tool):
    name = "team_status"
    description = "读取当前 Team、成员占用与 task 计数摘要。"
    category = "read"
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, manager: TeamManager) -> None:
        self._manager = manager

    async def execute(self, arguments: dict[str, Any]) -> object:
        validate_arguments(arguments, required={})
        try:
            result = _team_summary(await self._manager.get_team())
            integration = await self._manager.integration_status()
            result["integration_status"] = {
                "exists": integration.exists,
                "head": integration.head,
                "dirty": integration.dirty,
                "has_unpushed": integration.has_unpushed,
                "reason_code": integration.reason_code,
            }
            return result
        except TeamError as exc:
            raise _raise_tool_error(exc) from exc


class TeamIntegrateTool(Tool):
    name = "team_integrate"
    description = "把一个 completed Team task 的分支安全合入 integration worktree。"
    category = "write"
    timeout_seconds = 120.0
    parameters = {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
        "additionalProperties": False,
    }

    def __init__(self, manager: TeamManager) -> None:
        self._manager = manager

    async def execute(self, arguments: dict[str, Any]) -> object:
        validate_arguments(arguments, required={"task_id": str})
        try:
            task = await self._manager.integrate_task(arguments["task_id"])
        except TeamError as exc:
            raise _raise_tool_error(exc) from exc
        return _task_summary(task, detailed=False)


def team_tools(manager: TeamManager) -> tuple[Tool, ...]:
    return (
        TeamCreateTool(manager),
        TeamTaskTool(manager),
        TeamMessageTool(manager),
        TeamStatusTool(manager),
        TeamIntegrateTool(manager),
    )
