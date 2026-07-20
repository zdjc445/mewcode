from __future__ import annotations

from collections.abc import AsyncIterator
import json

import pytest

from mewcode_agent.compaction import (
    SUMMARY_SYSTEM_PROMPT,
    ContextCompactionError,
    ContextSummarizer,
)
from mewcode_agent.models import ChatMessage, ToolCall
from mewcode_agent.providers.base import (
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderTurnEnd,
    ProviderUsageEvent,
    ProviderUsageResult,
)


def valid_summary_text() -> str:
    return json.dumps(
        {
            "analysis_draft": ["覆盖请求与待办"],
            "summary": {
                "primary_requests": ["实现压缩"],
                "key_concepts": ["两级压缩"],
                "files_and_code": ["src/example.py"],
                "errors_and_fixes": [],
                "solution_process": ["先写规范"],
                "pending_tasks": ["实现代码"],
                "current_work": ["编写摘要器"],
                "next_step": ["运行测试"],
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class SummaryProvider:
    provider_id = "summary"
    protocol = "openai"

    def __init__(self, events: tuple[ProviderStreamEvent, ...]) -> None:
        self.events = events
        self.requests: list[ProviderRequest] = []

    def prompt_payload(self, request: ProviderRequest) -> dict[str, object]:
        return {"messages": []}

    async def stream_chat(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        for event in self.events:
            yield event


def unavailable_usage() -> ProviderUsageEvent:
    return ProviderUsageEvent(
        ProviderUsageResult("unavailable", None, "test_usage_unavailable")
    )


@pytest.mark.asyncio
async def test_summarizer_uses_exact_tool_free_prompt_and_discards_draft() -> None:
    provider = SummaryProvider(
        (
            ProviderTextDelta(valid_summary_text()),
            unavailable_usage(),
            ProviderTurnEnd("end_turn"),
        )
    )
    summarizer = ContextSummarizer(
        provider,  # type: ignore[arg-type]
        timeout_seconds=1,
    )
    message = ChatMessage(role="user", content="原始请求\n保持空格  ")
    usage_results: list[ProviderUsageResult] = []

    result = await summarizer.summarize(
        previous=None,
        history_start=0,
        history_end=1,
        messages=(message,),
        on_usage=usage_results.append,
    )

    request = provider.requests[0]
    assert request.tools is None
    assert request.system_prompt.splitlines()[0] == (
        "禁止调用任何工具。本次请求只允许基于输入数据生成上下文压缩摘要。"
    )
    prompt_lines = request.system_prompt.splitlines()
    assert prompt_lines[-1] == prompt_lines[0]
    source = json.loads(request.items[0].content)  # type: ignore[union-attr]
    assert source["messages"][0]["content"] == message.content
    assert result.sections.primary_requests == ("实现压缩",)
    assert not hasattr(result, "analysis_draft")
    assert usage_results == [unavailable_usage().result]


@pytest.mark.asyncio
async def test_summarizer_rejects_tool_call_without_executing_it() -> None:
    provider = SummaryProvider(
        (
            ProviderToolCall(ToolCall("call", "read_file", "{}")),
            unavailable_usage(),
            ProviderTurnEnd("tool_calls"),
        )
    )
    summarizer = ContextSummarizer(
        provider,  # type: ignore[arg-type]
        timeout_seconds=1,
    )

    with pytest.raises(ContextCompactionError) as caught:
        await summarizer.summarize(
            previous=None,
            history_start=0,
            history_end=1,
            messages=(ChatMessage(role="user", content="任务"),),
        )

    assert caught.value.code == "context_summary_tool_call_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "```json\n{}\n```",
        json.dumps(
            {
                "summary": {},
                "analysis_draft": [],
            }
        ),
        json.dumps(
            {
                "analysis_draft": [],
                "summary": {
                    "primary_requests": [],
                    "key_concepts": [],
                },
            }
        ),
    ],
)
async def test_summarizer_rejects_invalid_json_contract(text: str) -> None:
    provider = SummaryProvider(
        (
            ProviderTextDelta(text),
            unavailable_usage(),
            ProviderTurnEnd("end_turn"),
        )
    )
    summarizer = ContextSummarizer(
        provider,  # type: ignore[arg-type]
        timeout_seconds=1,
    )

    with pytest.raises(ContextCompactionError) as caught:
        await summarizer.summarize(
            previous=None,
            history_start=0,
            history_end=1,
            messages=(ChatMessage(role="user", content="任务"),),
        )

    assert caught.value.code == "context_summary_invalid"


@pytest.mark.asyncio
async def test_summarizer_rejects_missing_usage_or_non_end_turn() -> None:
    missing_usage = SummaryProvider(
        (ProviderTextDelta(valid_summary_text()), ProviderTurnEnd("end_turn"))
    )
    non_end_turn = SummaryProvider(
        (
            ProviderTextDelta(valid_summary_text()),
            unavailable_usage(),
            ProviderTurnEnd("max_tokens"),
        )
    )

    for provider in (missing_usage, non_end_turn):
        with pytest.raises(ContextCompactionError) as caught:
            await ContextSummarizer(
                provider,  # type: ignore[arg-type]
                timeout_seconds=1,
            ).summarize(
                previous=None,
                history_start=0,
                history_end=1,
                messages=(ChatMessage(role="user", content="任务"),),
            )
        assert caught.value.code == "context_summary_invalid"
