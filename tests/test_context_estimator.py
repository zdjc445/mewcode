from __future__ import annotations

from mewcode_agent.compaction import CompactionConfig, ContextTokenEstimator
from mewcode_agent.models import ChatMessage
from mewcode_agent.providers.base import (
    ProviderRequest,
    ProviderUsage,
    ProviderUsageResult,
)


class PayloadProvider:
    provider_id = "test"
    protocol = "openai"

    def prompt_payload(self, request: ProviderRequest) -> dict[str, object]:
        return {
            "model": "model",
            "messages": [
                {"role": "system", "content": request.system_prompt},
                *(
                    {"role": item.role, "content": item.content}
                    for item in request.items
                    if isinstance(item, ChatMessage)
                ),
            ],
            "tools": list(request.tools or ()),
        }


def test_estimator_uses_full_utf8_fallback_without_usage() -> None:
    provider = PayloadProvider()
    estimator = ContextTokenEstimator(
        config=CompactionConfig(framing_safety_tokens=10)
    )
    request = ProviderRequest(
        "system",
        (ChatMessage(role="user", content="问题"),),
        None,
    )

    estimate = estimator.estimate(provider, request)  # type: ignore[arg-type]

    assert estimate.used_actual_baseline is False
    assert estimate.estimated_prompt_tokens > 10


def test_estimator_uses_actual_baseline_for_appended_messages() -> None:
    provider = PayloadProvider()
    config = CompactionConfig(framing_safety_tokens=10)
    estimator = ContextTokenEstimator(config=config)
    first = ProviderRequest(
        "system",
        (ChatMessage(role="user", content="第一问"),),
        None,
    )
    usage = ProviderUsageResult(
        "available",
        ProviderUsage(100, 40, 60, 5),
        None,
    )
    estimator.record_usage(provider, first, usage)  # type: ignore[arg-type]
    second = ProviderRequest(
        "system",
        (
            ChatMessage(role="user", content="第一问"),
            ChatMessage(role="assistant", content="第一答"),
        ),
        None,
    )

    estimate = estimator.estimate(provider, second)  # type: ignore[arg-type]

    assert estimate.used_actual_baseline is True
    assert estimate.estimated_prompt_tokens > 110


def test_estimator_falls_back_when_tools_change() -> None:
    provider = PayloadProvider()
    estimator = ContextTokenEstimator(
        config=CompactionConfig(framing_safety_tokens=10)
    )
    first = ProviderRequest(
        "system",
        (ChatMessage(role="user", content="问题"),),
        None,
    )
    estimator.record_usage(
        provider,  # type: ignore[arg-type]
        first,
        ProviderUsageResult(
            "available",
            ProviderUsage(100, 0, 100, 1),
            None,
        ),
    )
    changed = ProviderRequest(
        "system",
        first.items,
        ({"type": "function", "function": {"name": "tool"}},),
    )

    estimate = estimator.estimate(provider, changed)  # type: ignore[arg-type]

    assert estimate.used_actual_baseline is False


def test_estimator_reset_discards_previous_session_baseline() -> None:
    provider = PayloadProvider()
    estimator = ContextTokenEstimator(
        config=CompactionConfig(framing_safety_tokens=10)
    )
    request = ProviderRequest(
        "system",
        (ChatMessage(role="user", content="问题"),),
        None,
    )
    estimator.record_usage(
        provider,  # type: ignore[arg-type]
        request,
        ProviderUsageResult(
            "available",
            ProviderUsage(100, 0, 100, 1),
            None,
        ),
    )
    estimator.reset_session()

    estimate = estimator.estimate(provider, request)  # type: ignore[arg-type]

    assert estimate.used_actual_baseline is False
