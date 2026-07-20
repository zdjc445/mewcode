from __future__ import annotations

from collections.abc import AsyncIterator
import json
from pathlib import Path
from typing import Any

import pytest

from mewcode_agent.agent.context import AgentRunContext
from mewcode_agent.agent.events import ToolApprovalRequestedEvent
from mewcode_agent.agent.tool_scheduler import ToolScheduler
from mewcode_agent.compaction import ContextSummarizer
from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage, ToolCall
from mewcode_agent.prompting.composer import PromptComposer
from mewcode_agent.prompting.environment import (
    GitEnvironment,
    RequestEnvironment,
    SessionEnvironment,
)
from mewcode_agent.prompting.models import (
    ContextBoundaryMessage,
    ContextSummaryMessage,
    PromptModule,
)
from mewcode_agent.prompting.runtime import PromptRuntime
from mewcode_agent.providers.base import (
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderTurnEnd,
    ProviderUsageEvent,
    ProviderUsageResult,
)
from mewcode_agent.skills import (
    IsolatedSkillExecutor,
    LoadSkillTool,
    SkillCatalog,
    SkillConfigError,
    SkillRuntime,
    reject_isolated_approval,
    scan_skill_catalog,
)
from mewcode_agent.tools import Tool, ToolRegistry


SUMMARY_JSON = json.dumps(
    {
        "analysis_draft": ["covered"],
        "summary": {
            "primary_requests": ["request"],
            "key_concepts": [],
            "files_and_code": [],
            "errors_and_fixes": [],
            "solution_process": [],
            "pending_tasks": [],
            "current_work": [],
            "next_step": [],
        },
    },
    ensure_ascii=False,
)


class FixedCollector:
    async def collect(self) -> RequestEnvironment:
        return RequestEnvironment(
            "2026-07-20T12:00:00+08:00",
            GitEnvironment("repository", "master", "", None),
        )


class StubTool(Tool):
    description = "stub"
    parameters = {"type": "object"}
    category = "read"

    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, arguments: dict[str, Any]) -> Any:
        return arguments


class ScriptedProvider:
    provider_id = "scripted"
    protocol = "openai"

    def __init__(
        self,
        responses: list[tuple[ProviderStreamEvent, ...]],
    ) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    def prompt_payload(self, request: ProviderRequest) -> dict[str, Any]:
        return {}

    async def stream_chat(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        response = self.responses.pop(0)
        for event in response:
            yield event


def completed_response(text: str) -> tuple[ProviderStreamEvent, ...]:
    return (
        ProviderTextDelta(text),
        ProviderUsageEvent(
            ProviderUsageResult("unavailable", None, "test_usage")
        ),
        ProviderTurnEnd("end_turn"),
    )


def tool_call_response(
    call: ToolCall,
) -> tuple[ProviderStreamEvent, ...]:
    return (
        ProviderToolCall(call),
        ProviderUsageEvent(
            ProviderUsageResult("unavailable", None, "test_usage")
        ),
        ProviderTurnEnd("tool_calls"),
    )


@pytest.mark.asyncio
async def test_isolated_approval_handler_rejects_pending_request() -> None:
    context = AgentRunContext()
    context.begin_run()
    request_id = context.open_tool_approval()
    event = ToolApprovalRequestedEvent(
        request_id,
        "call_1",
        "run_command",
        "{}",
        "command",
    )

    reject_isolated_approval(event, context)

    assert await context.wait_for_tool_approval(request_id) == "reject"


def skill_document(
    *,
    strategy: str,
    recent_messages: str,
) -> str:
    return f"""---
name: isolated
description: Isolated skill
allowed_tools:
  - read_file
execution_mode: isolated
model: inherit
context_strategy: {strategy}
recent_messages: {recent_messages}
---
ISOLATED SOP SECRET
"""


def build_executor(
    tmp_path: Path,
    *,
    strategy: str,
    recent_messages: str,
    provider: ScriptedProvider,
    main_history: ConversationHistory,
) -> tuple[SkillRuntime, PromptRuntime]:
    project_root = tmp_path / "project"
    skill_root = project_root / ".mewcode" / "skills"
    skill_root.mkdir(parents=True)
    (skill_root / "isolated.md").write_text(
        skill_document(
            strategy=strategy,
            recent_messages=recent_messages,
        ),
        encoding="utf-8",
    )
    (skill_root / "nested.md").write_text(
        """---
name: nested
description: Nested shared skill
allowed_tools:
  - read_file
execution_mode: shared
model: inherit
context_strategy: current
recent_messages: null
---
NESTED SHARED SOP
""",
        encoding="utf-8",
    )
    builtin_root = tmp_path / "builtin"
    builtin_root.mkdir()
    registry = ToolRegistry()
    registry.register(StubTool("read_file"))
    snapshot = scan_skill_catalog(
        project_root=project_root,
        user_root=tmp_path / "user",
        builtin_root=builtin_root,
        existing_tool_names=registry.tool_names(),
        reserved_command_names=("skills",),
    )
    prompt_runtime = PromptRuntime(
        SessionEnvironment(
            "Windows",
            "powershell.exe",
            str(project_root.resolve()),
            "China Standard Time",
            "+08:00",
        ),
        FixedCollector(),
    )
    runtime = SkillRuntime(
        SkillCatalog(snapshot),
        registry,
        prompt_runtime,
        reserved_command_names=("skills",),
    )
    load_tool = LoadSkillTool(runtime)
    registry.register(load_tool)
    scheduler = ToolScheduler(registry)
    composer = PromptComposer(
        (PromptModule("base", 0, "base prompt", "builtin", True),)
    )
    executor = IsolatedSkillExecutor(
        provider=provider,
        registry=registry,
        scheduler=scheduler,
        prompt_runtime=prompt_runtime,
        prompt_composer=composer,
        skill_runtime=runtime,
        load_skill_tool=load_tool,
        main_history=main_history,
        summarizer=ContextSummarizer(provider, timeout_seconds=5),
        approval_handler=reject_isolated_approval,
    )
    runtime.set_isolated_runner(executor.run)
    return runtime, prompt_runtime


@pytest.mark.asyncio
async def test_none_strategy_uses_independent_history_and_returns_only_final(
    tmp_path: Path,
) -> None:
    history = ConversationHistory()
    history.add_user("MAIN HISTORY SECRET")
    before = history.snapshot()
    provider = ScriptedProvider([completed_response("isolated result")])
    runtime, _prompt_runtime = build_executor(
        tmp_path,
        strategy="none",
        recent_messages="null",
        provider=provider,
        main_history=history,
    )

    result = await runtime.load("isolated", "Exact ARG")

    assert result == {
        "name": "isolated",
        "execution_mode": "isolated",
        "result": "isolated result",
    }
    assert history.snapshot() == before
    request = provider.requests[0]
    request_text = "\n".join(
        item.content for item in request.items if hasattr(item, "content")
    )
    assert "ISOLATED SOP SECRET" in request_text
    assert "Exact ARG" in request_text
    assert "MAIN HISTORY SECRET" not in request_text
    assert request.tools is not None
    assert [tool["function"]["name"] for tool in request.tools] == [
        "read_file",
        "load_skill",
    ]


@pytest.mark.asyncio
async def test_recent_strategy_expands_to_complete_tool_transaction(
    tmp_path: Path,
) -> None:
    history = ConversationHistory()
    history.add_user("old user")
    history.add_assistant_tool_call(
        "",
        ToolCall("call_1", "read_file", '{"path":"a"}'),
    )
    from mewcode_agent.tools import ToolResult

    history.add_tool_result(
        "call_1",
        ToolResult("read_file", True, {"content": "tool result"}),
    )
    history.add_user("latest user")
    provider = ScriptedProvider([completed_response("recent result")])
    runtime, _ = build_executor(
        tmp_path,
        strategy="recent",
        recent_messages="2",
        provider=provider,
        main_history=history,
    )

    await runtime.load("isolated", "")

    request = provider.requests[0]
    chat_items = [item for item in request.items if hasattr(item, "role")]
    assert [item.role for item in chat_items] == [
        "assistant",
        "tool",
        "user",
        "user",
    ]
    assert "old user" not in [item.content for item in chat_items]
    assert chat_items[1].tool_call_id == "call_1"


@pytest.mark.asyncio
async def test_summary_strategy_uses_tool_free_summary_and_boundary_prefix(
    tmp_path: Path,
) -> None:
    history = ConversationHistory()
    history.add_user("verbatim user request")
    history.add_assistant("assistant detail")
    provider = ScriptedProvider(
        [
            completed_response(SUMMARY_JSON),
            completed_response("summary result"),
        ]
    )
    runtime, _ = build_executor(
        tmp_path,
        strategy="summary",
        recent_messages="null",
        provider=provider,
        main_history=history,
    )

    result = await runtime.load("isolated", "")

    assert result["result"] == "summary result"
    assert len(provider.requests) == 2
    summary_request, isolated_request = provider.requests
    assert summary_request.tools is None
    assert any(
        isinstance(item, ContextSummaryMessage)
        for item in isolated_request.items
    )
    assert any(
        isinstance(item, ContextBoundaryMessage)
        for item in isolated_request.items
    )
    summary_item = next(
        item
        for item in isolated_request.items
        if isinstance(item, ContextSummaryMessage)
    )
    assert "verbatim user request" in summary_item.content_json
    chat_content = [
        item.content
        for item in isolated_request.items
        if hasattr(item, "role")
    ]
    assert "assistant detail" not in chat_content


@pytest.mark.asyncio
async def test_invalid_recent_history_returns_stable_isolated_error(
    tmp_path: Path,
) -> None:
    history = ConversationHistory()
    history.restore(
        (
            ChatMessage(
                role="tool",
                content="{}",
                tool_call_id="orphan",
            ),
        )
    )
    provider = ScriptedProvider([completed_response("unused")])
    runtime, _ = build_executor(
        tmp_path,
        strategy="recent",
        recent_messages="1",
        provider=provider,
        main_history=history,
    )

    with pytest.raises(SkillConfigError) as caught:
        await runtime.load("isolated", "")

    assert caught.value.code == "skill_isolated_failed"
    assert provider.requests == []


@pytest.mark.asyncio
async def test_nested_load_skill_binds_to_isolated_runtime_only(
    tmp_path: Path,
) -> None:
    history = ConversationHistory()
    provider = ScriptedProvider(
        [
            tool_call_response(
                ToolCall(
                    "nested_call",
                    "load_skill",
                    '{"name":"nested","arguments":"nested arg"}',
                )
            ),
            completed_response("nested result"),
        ]
    )
    runtime, _ = build_executor(
        tmp_path,
        strategy="none",
        recent_messages="null",
        provider=provider,
        main_history=history,
    )

    result = await runtime.load("isolated", "")

    assert result["result"] == "nested result"
    assert runtime.active_skills == ()
    second_request_text = "\n".join(
        item.content
        for item in provider.requests[1].items
        if hasattr(item, "content")
    )
    assert "NESTED SHARED SOP" in second_request_text
    assert "nested arg" in second_request_text
