from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mewcode_agent.agent.usage import UsageRecord
from mewcode_agent.compaction import ContextCompactionError
from mewcode_agent.models import ChatMessage, ToolCall
from mewcode_agent.prompting.builtins import BUILTIN_MODULES
from mewcode_agent.prompting.composer import PromptComposer
from mewcode_agent.prompting.environment import (
    GitEnvironment,
    RequestEnvironment,
    SessionEnvironment,
)
from mewcode_agent.prompting.models import RuntimeInstruction
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.providers.base import (
    ProviderProtocol,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderTurnEnd,
    ProviderUsage,
    ProviderUsageEvent,
    ProviderUsageResult,
)
from mewcode_agent.security import (
    PathSandbox,
    SecurityBoundary,
    SecurityConfiguration,
    SecurityPolicyEngine,
)
from mewcode_agent.tools import Tool, ToolRegistry
from mewcode_agent.workers import (
    WorkerExecutionSpec,
    WorkerExecutor,
    WorkerRoleDefinition,
    WorkerRuntimeConfig,
    WorkerUsageCollector,
    fork_history_prefix,
    fork_report_format_valid,
    visible_worker_tools,
)


class FixedCollector:
    async def collect(self) -> RequestEnvironment:
        return RequestEnvironment(
            "2026-07-21T12:00:00+08:00",
            GitEnvironment("not_repository", None, None, None),
        )


def make_runtime() -> PromptRuntime:
    return PromptRuntime(
        SessionEnvironment(
            "Windows",
            "powershell.exe",
            "D:\\workspace",
            "China Standard Time",
            "+08:00",
        ),
        FixedCollector(),
        session_controls=(
            RuntimeInstruction(
                "runtime.instructions.project",
                "instruction",
                "session",
                "project instruction",
                "project",
            ),
        ),
    )


class ScriptedProvider:
    def __init__(self, rounds: list[list[ProviderStreamEvent]]) -> None:
        self.rounds = rounds
        self.requests: list[ProviderRequest] = []

    @property
    def provider_id(self) -> str:
        return "provider-a"

    @property
    def protocol(self) -> ProviderProtocol:
        return "openai"

    async def stream_chat(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        for event in self.rounds.pop(0):
            yield event


def completed_round(
    content: str,
    *,
    usage: ProviderUsage | None = None,
) -> list[ProviderStreamEvent]:
    return [
        ProviderTextDelta(content),
        ProviderUsageEvent(
            ProviderUsageResult(
                "available",
                usage or ProviderUsage(0, 0, 0, 0),
                None,
            )
        ),
        ProviderTurnEnd("end_turn"),
    ]


def role(tmp_path: Path, **changes: object) -> WorkerRoleDefinition:
    values: dict[str, object] = {
        "name": "example",
        "description": "Example worker",
        "allowed_tools": None,
        "denied_tools": ("spawn_worker",),
        "model": "inherit",
        "max_rounds": 5,
        "permission_mode": "inherit",
        "isolation": "none",
        "body": "Use exact evidence.",
        "source": "project",
        "source_root": tmp_path.resolve(),
        "source_path": (tmp_path / "example.md").resolve(),
    }
    values.update(changes)
    return WorkerRoleDefinition(**values)  # type: ignore[arg-type]


def spec(
    tmp_path: Path,
    *,
    kind: str = "definition",
    definition: WorkerRoleDefinition | None = None,
    history: tuple[ChatMessage, ...] = (),
    visible: frozenset[str] = frozenset(),
) -> WorkerExecutionSpec:
    actual_definition = (
        role(tmp_path) if definition is None and kind == "definition" else definition
    )
    return WorkerExecutionSpec(
        "a" * 32,
        "session-a",
        "fork" if kind == "fork" else "example",
        kind,  # type: ignore[arg-type]
        "Inspect this task exactly.",
        actual_definition,
        history,
        visible,
        "provider-a",
        "model-a",
    )


def make_executor(
    provider: ScriptedProvider,
    registry: ToolRegistry,
    tmp_path: Path,
    *,
    strict_policy: bool = False,
) -> WorkerExecutor:
    boundary = SecurityBoundary(PathSandbox(tmp_path))

    def policy(_mode: str) -> SecurityPolicyEngine | None:
        if not strict_policy:
            return None
        return SecurityPolicyEngine(
            SecurityConfiguration("strict", (), ()),
            boundary,
        )

    return WorkerExecutor(
        registry=registry,
        parent_prompt_runtime=make_runtime(),
        prompt_composer=PromptComposer(BUILTIN_MODULES),
        provider_resolver=lambda provider_id: (
            provider if provider_id == "provider-a" else None
        ),  # type: ignore[arg-type,return-value]
        policy_engine_factory=policy,  # type: ignore[arg-type]
        context_manager_factory=lambda _provider: None,
    )


def test_fork_history_drops_current_incomplete_tool_batch() -> None:
    history = (
        ChatMessage("user", "old task"),
        ChatMessage(
            "assistant",
            "",
            (
                ToolCall("call-a", "read_file", "{}"),
                ToolCall("call-b", "read_file", "{}"),
            ),
        ),
        ChatMessage("tool", "{}", tool_call_id="call-a"),
    )

    assert fork_history_prefix(history) == history[:1]


def test_fork_history_keeps_complete_tool_batch() -> None:
    history = (
        ChatMessage(
            "assistant",
            "",
            (ToolCall("call-a", "read_file", "{}"),),
        ),
        ChatMessage("tool", "{}", tool_call_id="call-a"),
    )

    assert fork_history_prefix(history) == history


def test_fork_history_rejects_invalid_earlier_batch() -> None:
    history = (
        ChatMessage(
            "assistant",
            "",
            (ToolCall("call-a", "read_file", "{}"),),
        ),
        ChatMessage("tool", "{}", tool_call_id="wrong"),
        ChatMessage("user", "later"),
        ChatMessage(
            "assistant",
            "",
            (ToolCall("call-b", "read_file", "{}"),),
        ),
        ChatMessage("tool", "{}", tool_call_id="call-b"),
    )

    with pytest.raises(ContextCompactionError):
        fork_history_prefix(history)


def test_visible_tools_only_shrink_in_registry_order(tmp_path: Path) -> None:
    definition = role(
        tmp_path,
        allowed_tools=None,
        denied_tools=("run_command",),
    )

    visible = visible_worker_tools(
        ("spawn_worker", "run_command", "read_file", "write_file"),
        base_visible_tools=frozenset(
            {"spawn_worker", "run_command", "read_file"}
        ),
        definition=definition,
        background=True,
        runtime_config=WorkerRuntimeConfig(
            background_allowed_tools=("read_file",)
        ),
    )

    assert visible == frozenset({"read_file"})


@pytest.mark.parametrize(
    ("content", "valid"),
    [
        (
            "## Summary\na\n## Evidence\nb\n## Risks\nc\n## Next Steps\nd",
            True,
        ),
        ("## Summary\na\n## Risks\nc\n## Next Steps\nd", False),
        (
            "## Evidence\nb\n## Summary\na\n## Risks\nc\n## Next Steps\nd",
            False,
        ),
        (
            "## Summary\n" + "x" * 1200 + "\n## Evidence\n## Risks\n## Next Steps",
            False,
        ),
    ],
)
def test_fork_report_validation(content: str, valid: bool) -> None:
    assert fork_report_format_valid(content) is valid


async def test_definition_executor_uses_empty_history_and_role_control(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [completed_round("done", usage=ProviderUsage(7, 2, 5, 3))]
    )
    usage = WorkerUsageCollector()

    outcome = await make_executor(
        provider,
        ToolRegistry(),
        tmp_path,
    ).run(spec(tmp_path), usage)

    assert outcome.result == "done"
    assert outcome.report_format_valid is True
    messages = [item for item in provider.requests[0].items if isinstance(item, ChatMessage)]
    assert [message.role for message in messages] == ["user"]
    assert "任务（原文）" in messages[0].content
    controls = [
        item for item in provider.requests[0].items if not isinstance(item, ChatMessage)
    ]
    assert any("Use exact evidence." in item.content for item in controls)
    assert usage.snapshot().to_dict() == {
        "prompt_tokens": 7,
        "cache_hit_tokens": 2,
        "cache_miss_tokens": 5,
        "completion_tokens": 3,
        "unavailable_rounds": 0,
    }


async def test_fork_executor_copies_only_complete_parent_prefix(
    tmp_path: Path,
) -> None:
    report = "## Summary\ndone\n## Evidence\nfile\n## Risks\nnone\n## Next Steps\nnone"
    provider = ScriptedProvider([completed_round(report)])
    history = (
        ChatMessage("user", "parent original"),
        ChatMessage(
            "assistant",
            "",
            (ToolCall("current", "spawn_worker", "{}"),),
        ),
    )

    outcome = await make_executor(provider, ToolRegistry(), tmp_path).run(
        spec(tmp_path, kind="fork", history=history),
        WorkerUsageCollector(),
    )

    assert outcome.report_format_valid is True
    messages = [item for item in provider.requests[0].items if isinstance(item, ChatMessage)]
    assert [message.content for message in messages] == [
        "parent original",
        messages[-1].content,
    ]
    assert "Fork 子工作者任务" in messages[-1].content
    assert all(
        not message.tool_calls or message.tool_calls[0].name != "spawn_worker"
        for message in messages
    )


class RecordingReadTool(Tool):
    name = "read_test"
    description = "test"
    category = "read"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, arguments: dict[str, Any]) -> Any:
        self.executions += 1
        return arguments


async def test_worker_auto_rejects_tool_approval(tmp_path: Path) -> None:
    tool = RecordingReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    provider = ScriptedProvider(
        [
            [
                ProviderToolCall(ToolCall("call", "read_test", "{}")),
                ProviderUsageEvent(
                    ProviderUsageResult("unavailable", None, "not reported")
                ),
                ProviderTurnEnd("tool_calls"),
            ],
            completed_round("completed after rejection"),
        ]
    )

    outcome = await make_executor(
        provider,
        registry,
        tmp_path,
        strict_policy=True,
    ).run(
        spec(tmp_path, visible=frozenset({"read_test"})),
        WorkerUsageCollector(),
    )

    assert outcome.result == "completed after rejection"
    assert tool.executions == 0
    tool_results = [
        item for item in provider.requests[1].items if isinstance(item, ChatMessage) and item.role == "tool"
    ]
    assert len(tool_results) == 1
    assert "tool_denied_by_user" in tool_results[0].content


def test_worker_usage_counts_unavailable_rounds() -> None:
    collector = WorkerUsageCollector()
    collector.record(
        UsageRecord(
            "provider-a",
            1,
            1,
            "executing",
            ProviderUsageResult("unavailable", None, "missing"),
        )
    )
    collector.record(
        UsageRecord(
            "provider-a",
            1,
            2,
            "executing",
            ProviderUsageResult("invalid", None, "bad"),
        )
    )

    assert collector.snapshot().unavailable_rounds == 2
