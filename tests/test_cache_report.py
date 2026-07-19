from collections.abc import AsyncIterator
from datetime import datetime, timezone
import json
from pathlib import Path

from integration_tests.cache_report import (
    CacheScenario,
    write_cache_report,
)
from integration_tests.test_prompt_cache import _scenario
from mewcode_agent.agent.usage import UsageRecord
from mewcode_agent.providers.base import (
    ProviderProtocol,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTurnEnd,
    ProviderUsage,
    ProviderUsageEvent,
    ProviderUsageResult,
)


class ZeroUsageProvider:
    @property
    def provider_id(self) -> str:
        return "deepseek_openai"

    @property
    def protocol(self) -> ProviderProtocol:
        return "openai"

    async def stream_chat(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderUsageEvent(
            ProviderUsageResult(
                "available",
                ProviderUsage(0, 0, 0, 0),
                None,
            )
        )
        yield ProviderTurnEnd("end_turn")


def test_cache_report_has_exact_schema_and_null_rules(
    tmp_path: Path,
) -> None:
    path = tmp_path / "report.json"
    scenarios = (
        CacheScenario(
            "stable_prefix_repeat",
            "deepseek_anthropic",
            (
                (
                    1,
                    UsageRecord(
                        "deepseek_anthropic",
                        1,
                        1,
                        "executing",
                        ProviderUsageResult(
                            "available",
                            ProviderUsage(1543, 1536, 7, 13),
                            None,
                        ),
                    ),
                ),
                (
                    2,
                    UsageRecord(
                        "deepseek_anthropic",
                        2,
                        1,
                        "executing",
                        ProviderUsageResult(
                            "unavailable",
                            None,
                            "usage_missing",
                        ),
                    ),
                ),
            ),
        ),
    )

    write_cache_report(
        path,
        model="deepseek-v4-pro",
        scenarios=scenarios,
        generated_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    result = json.loads(path.read_text(encoding="utf-8"))

    assert tuple(result) == (
        "schema_version",
        "generated_at",
        "model",
        "scenarios",
    )
    first, second = result["scenarios"][0]["attempts"]
    assert first["cache_hit_rate"] == 1536 / 1543
    assert first["reason"] is None
    assert second["prompt_tokens"] is None
    assert second["cache_hit_tokens"] is None
    assert second["cache_miss_tokens"] is None
    assert second["completion_tokens"] is None
    assert second["cache_hit_rate"] is None
    assert second["reason"] == "usage_missing"
    serialized = path.read_text(encoding="utf-8")
    assert "API Key" not in serialized
    assert "system_prompt" not in serialized
    assert "user_message" not in serialized
    assert "thinking" not in serialized


def test_zero_prompt_tokens_use_null_hit_rate(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    scenario = CacheScenario(
        "stable_prefix_repeat",
        "deepseek_openai",
        (
            (
                1,
                UsageRecord(
                    "deepseek_openai",
                    1,
                    1,
                    "executing",
                    ProviderUsageResult(
                        "available",
                        ProviderUsage(0, 0, 0, 0),
                        None,
                    ),
                ),
            ),
        ),
    )

    write_cache_report(
        path,
        model="deepseek-v4-pro",
        scenarios=(scenario,),
        generated_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["scenarios"][0]["attempts"][0]["cache_hit_rate"] is None


async def test_cache_scenario_preserves_planning_mode() -> None:
    request = ProviderRequest("system", (), None)

    scenario = await _scenario(
        ZeroUsageProvider(),
        "round_reminder_append",
        (request,),
        (1,),
        mode="planning",
    )

    assert scenario.attempts[0][1].mode == "planning"
