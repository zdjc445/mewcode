from mewcode_agent.agent.events import AgentEvent
from mewcode_agent.agent.usage import (
    CompactionUsageRecord,
    NoteUsageRecord,
    UsageRecord,
)
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


def test_compaction_usage_record_has_separate_request_kind() -> None:
    result = ProviderUsageResult(
        "available",
        ProviderUsage(10, 8, 2, 1),
        None,
    )
    record = CompactionUsageRecord(
        provider_id="deepseek_openai",
        generation=2,
        result=result,
    )

    assert record.request_kind == "compaction"
    assert record.generation == 2
    assert not hasattr(record, "request_sequence")
    assert UsageRecord(
        "deepseek_openai",
        1,
        1,
        "executing",
        result,
    ).request_kind == "agent"


def test_note_usage_record_has_distinct_request_kind() -> None:
    result = ProviderUsageResult("unavailable", None, "not reported")
    record = NoteUsageRecord("provider", 2, result)

    assert record.request_kind == "notes"
    assert record.generation == 2
    assert not hasattr(record, "request_sequence")
