from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

import pytest

from integration_tests.cache_report import CacheScenario, write_cache_report
from mewcode_agent.agent.events import AgentRunMode
from mewcode_agent.agent.usage import UsageRecord
from mewcode_agent.config import load_config
from mewcode_agent.models import ChatMessage
from mewcode_agent.prompting.builtins import (
    BUILTIN_MODULES,
    EXECUTION_MODE_TEXT,
    PLANNING_REMINDER_TEXT,
)
from mewcode_agent.prompting.composer import PromptComposer
from mewcode_agent.prompting.models import ControlMessage
from mewcode_agent.providers.base import (
    LLMProvider,
    ProviderRequest,
    ProviderTurnEnd,
    ProviderUsageEvent,
)
from mewcode_agent.providers.factory import create_provider
from mewcode_agent.tools.registry import create_core_registry

pytestmark = pytest.mark.integration

REPORT_PATH = Path.cwd() / ".pytest-tmp" / "ch03-cache-report.json"
LONG_PREFIX = "缓存评估使用固定且不含用户数据的前缀。" * 800
SCENARIO_IDS = (
    "stable_prefix_repeat",
    "request_environment_change",
    "round_reminder_append",
    "equivalent_protocol_controls",
    "tool_definition_change",
)


class RecordingUsageCollector:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    def record(self, record: UsageRecord) -> None:
        self.records.append(record)


def _request_environment(current_time: str) -> str:
    return json.dumps(
        {
            "current_time": current_time,
            "git": {
                "state": "not_repository",
                "branch": None,
                "worktree_status": None,
                "reason": None,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _provider_request(
    *,
    current_time: str,
    include_reminder: bool,
    tools: tuple[dict[str, Any], ...] | None,
    mode: str = "executing",
) -> ProviderRequest:
    if mode not in ("planning", "executing"):
        raise ValueError("mode 必须为 planning 或 executing")
    history = [
        ChatMessage(
            role="user",
            content=LONG_PREFIX + "\n只回复 OK。",
        )
    ]
    timeline = [
        ControlMessage(
            "runtime.environment.session",
            "context",
            "session",
            json.dumps(
                {
                    "operating_system": "integration_test",
                    "shell": "/bin/sh",
                    "working_directory": "integration_test",
                    "timezone": {
                        "name": None,
                        "utc_offset": "+00:00",
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            1,
            0,
            None,
            None,
        ),
        ControlMessage(
            "runtime.environment.request_1",
            "context",
            "request",
            _request_environment(current_time),
            2,
            0,
            1,
            None,
        ),
    ]
    next_sequence = 3
    if mode == "executing":
        timeline.append(
            ControlMessage(
                "runtime.mode.execution.request_1",
                "instruction",
                "request",
                EXECUTION_MODE_TEXT,
                next_sequence,
                0,
                1,
                None,
            )
        )
        next_sequence += 1
    timeline.append(
        ControlMessage(
            "runtime.state.request_1.round_1",
            "state",
            "round",
            f"当前运行状态：request=1，round=1/15，mode={mode}。",
            next_sequence,
            1,
            1,
            1,
        )
    )
    next_sequence += 1
    if include_reminder:
        if mode != "planning":
            raise ValueError("planning reminder 只允许 planning mode")
        timeline.append(
            ControlMessage(
                "runtime.mode.planning_reminder.request_1.round_1",
                "instruction",
                "round",
                PLANNING_REMINDER_TEXT,
                next_sequence,
                1,
                1,
                1,
            )
        )
    frame = PromptComposer(BUILTIN_MODULES).compose(
        history,
        tuple(timeline),
    )
    return ProviderRequest(frame.system_prompt, frame.items, tools)


def _extra_tool(protocol: str) -> dict[str, Any]:
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    if protocol == "openai":
        return {
            "type": "function",
            "function": {
                "name": "cache_test_noop",
                "description": "缓存评估使用的固定测试工具。",
                "parameters": parameters,
            },
        }
    if protocol == "anthropic":
        return {
            "name": "cache_test_noop",
            "description": "缓存评估使用的固定测试工具。",
            "input_schema": parameters,
        }
    raise ValueError(f"不支持的 Provider protocol: {protocol}")


async def _collect_usage(
    provider: LLMProvider,
    request: ProviderRequest,
    *,
    request_sequence: int,
    mode: AgentRunMode,
    collector: RecordingUsageCollector,
) -> UsageRecord:
    events = [event async for event in provider.stream_chat(request)]
    usage_events = [
        event for event in events if isinstance(event, ProviderUsageEvent)
    ]
    assert len(usage_events) == 1
    assert isinstance(events[-1], ProviderTurnEnd)
    assert events[-2] is usage_events[0]
    record = UsageRecord(
        provider.provider_id,
        request_sequence,
        1,
        mode,
        usage_events[0].result,
    )
    collector.record(record)
    return record


async def _scenario(
    provider: LLMProvider,
    scenario_id: str,
    requests: tuple[ProviderRequest, ...],
    request_sequences: tuple[int, ...],
    *,
    mode: AgentRunMode,
) -> CacheScenario:
    collector = RecordingUsageCollector()
    for request, request_sequence in zip(
        requests,
        request_sequences,
        strict=True,
    ):
        await _collect_usage(
            provider,
            request,
            request_sequence=request_sequence,
            mode=mode,
            collector=collector,
        )
    return CacheScenario(
        scenario_id,
        provider.provider_id,
        tuple(enumerate(collector.records, start=1)),
    )


@pytest.mark.asyncio
async def test_real_prompt_cache_report() -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY 未设置")
    config = load_config(Path.cwd() / "llm_providers.yaml")
    registry = create_core_registry()
    scenarios: list[CacheScenario] = []

    for provider_id in ("deepseek_openai", "deepseek_anthropic"):
        provider = create_provider(config.providers[provider_id], api_key)
        base_tools = tuple(registry.api_tools(provider.protocol))
        stable = _provider_request(
            current_time="2026-07-18T12:00:00+00:00",
            include_reminder=False,
            tools=None,
        )
        scenarios.append(
            await _scenario(
                provider,
                "stable_prefix_repeat",
                (stable, stable, stable),
                (1, 1, 1),
                mode="executing",
            )
        )
        scenarios.append(
            await _scenario(
                provider,
                "request_environment_change",
                (
                    _provider_request(
                        current_time="2026-07-18T12:00:00+00:00",
                        include_reminder=False,
                        tools=None,
                    ),
                    _provider_request(
                        current_time="2026-07-18T12:01:00+00:00",
                        include_reminder=False,
                        tools=None,
                    ),
                ),
                (1, 2),
                mode="executing",
            )
        )
        scenarios.append(
            await _scenario(
                provider,
                "round_reminder_append",
                (
                    _provider_request(
                        current_time="2026-07-18T12:00:00+00:00",
                        include_reminder=False,
                        tools=None,
                        mode="planning",
                    ),
                    _provider_request(
                        current_time="2026-07-18T12:00:00+00:00",
                        include_reminder=True,
                        tools=None,
                        mode="planning",
                    ),
                ),
                (1, 1),
                mode="planning",
            )
        )
        scenarios.append(
            await _scenario(
                provider,
                "equivalent_protocol_controls",
                (stable,),
                (1,),
                mode="executing",
            )
        )
        scenarios.append(
            await _scenario(
                provider,
                "tool_definition_change",
                (
                    _provider_request(
                        current_time="2026-07-18T12:00:00+00:00",
                        include_reminder=False,
                        tools=base_tools,
                    ),
                    _provider_request(
                        current_time="2026-07-18T12:00:00+00:00",
                        include_reminder=False,
                        tools=(
                            *base_tools,
                            _extra_tool(provider.protocol),
                        ),
                    ),
                ),
                (1, 1),
                mode="executing",
            )
        )

    assert len(scenarios) == 10
    assert {item.scenario_id for item in scenarios} == set(SCENARIO_IDS)
    for scenario in scenarios:
        for _, record in scenario.attempts:
            if record.result.status == "available":
                assert record.result.usage is not None
                usage = record.result.usage
                assert usage.prompt_tokens == (
                    usage.cache_hit_tokens + usage.cache_miss_tokens
                )
            else:
                assert record.result.usage is None
                assert record.result.reason
    write_cache_report(
        REPORT_PATH,
        model="deepseek-v4-pro",
        scenarios=tuple(scenarios),
        generated_at=datetime.now().astimezone(),
    )
    assert REPORT_PATH.is_file()
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert len(report["scenarios"]) == 10
