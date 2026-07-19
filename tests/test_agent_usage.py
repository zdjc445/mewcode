from mewcode_agent.agent.events import AgentEvent
from mewcode_agent.agent.usage import UsageRecord
from mewcode_agent.providers.base import ProviderUsage, ProviderUsageResult


def test_usage_record_contains_only_report_metadata() -> None:
    record = UsageRecord(
        provider_id="deepseek_openai",
        request_sequence=2,
        round_number=3,
        mode="planning",
        result=ProviderUsageResult(
            "available",
            ProviderUsage(10, 8, 2, 1),
            None,
        ),
    )

    assert tuple(record.__dataclass_fields__) == (
        "provider_id",
        "request_sequence",
        "round_number",
        "mode",
        "result",
    )
    assert not isinstance(record, AgentEvent)
