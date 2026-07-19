"""Immutable events emitted by the ReAct agent loop."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from mewcode_agent.tools.base import ToolCategory, ToolResult

AgentRunMode: TypeAlias = Literal["planning", "executing"]
AgentRunState: TypeAlias = Literal[
    "planning",
    "waiting_tool_approval",
    "waiting_plan_approval",
    "executing",
    "completed",
    "cancelled",
    "failed",
]
ToolApprovalDecision: TypeAlias = Literal[
    "allow_once",
    "allow_session",
    "allow_permanent",
    "reject",
]
PlanApprovalDecision: TypeAlias = Literal[
    "execute_current",
    "request_changes",
    "reject",
]


@dataclass(frozen=True, slots=True)
class UserMessageEvent:
    content: str


@dataclass(frozen=True, slots=True)
class RoundStartedEvent:
    round_number: int
    max_rounds: int
    mode: AgentRunMode


@dataclass(frozen=True, slots=True)
class ModelThinkingEvent:
    text: str


@dataclass(frozen=True, slots=True)
class ModelTextEvent:
    text: str


@dataclass(frozen=True, slots=True)
class ToolApprovalRequestedEvent:
    request_id: str
    call_id: str
    tool_name: str
    arguments_json: str
    category: ToolCategory
    reason_code: str = "approval_required"


@dataclass(frozen=True, slots=True)
class PlanApprovalRequestedEvent:
    request_id: str
    plan: str
    can_execute: bool
    can_request_changes: bool


@dataclass(frozen=True, slots=True)
class ToolCallStartedEvent:
    call_id: str
    tool_name: str
    arguments_json: str
    category: ToolCategory


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    call_id: str
    result: ToolResult


@dataclass(frozen=True, slots=True)
class FinalResponseEvent:
    content: str
    total_rounds: int


@dataclass(frozen=True, slots=True)
class RunErrorEvent:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RunCancelledEvent:
    reason: str


AgentEvent: TypeAlias = (
    UserMessageEvent
    | RoundStartedEvent
    | ModelThinkingEvent
    | ModelTextEvent
    | ToolApprovalRequestedEvent
    | PlanApprovalRequestedEvent
    | ToolCallStartedEvent
    | ToolResultEvent
    | FinalResponseEvent
    | RunErrorEvent
    | RunCancelledEvent
)
