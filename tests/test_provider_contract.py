from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mewcode_agent.models import ChatMessage
from mewcode_agent.providers.base import (
    ProviderRequest,
    ProviderUsage,
    ProviderUsageEvent,
    ProviderUsageResult,
)


def test_provider_request_is_frozen_and_keeps_tuple_items() -> None:
    request = ProviderRequest(
        "system",
        (ChatMessage(role="user", content="task"),),
        ({"type": "function"},),
    )

    assert request.items[0].role == "user"
    with pytest.raises(FrozenInstanceError):
        request.system_prompt = "changed"  # type: ignore[misc]


def test_available_usage_requires_exact_token_identity() -> None:
    usage = ProviderUsage(
        prompt_tokens=150,
        cache_hit_tokens=120,
        cache_miss_tokens=30,
        completion_tokens=9,
    )

    result = ProviderUsageResult("available", usage, None)

    assert ProviderUsageEvent(result).result.usage == usage


def test_usage_identity_allows_zero_but_rejects_mismatch() -> None:
    assert ProviderUsage(0, 0, 0, 0).prompt_tokens == 0

    with pytest.raises(ValueError, match="prompt_tokens"):
        ProviderUsage(149, 120, 30, 9)


@pytest.mark.parametrize(
    "values",
    [(-1, 0, 0, 0), (0, True, 0, 0), (0, 0, 1.5, 0)],
)
def test_usage_rejects_negative_bool_and_non_integer(
    values: tuple[object, object, object, object],
) -> None:
    with pytest.raises(ValueError):
        ProviderUsage(*values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "result",
    [
        ("available", None, None),
        ("available", ProviderUsage(0, 0, 0, 0), "reason"),
        ("unavailable", ProviderUsage(0, 0, 0, 0), "reason"),
        ("invalid", None, None),
    ],
)
def test_usage_result_rejects_status_payload_mismatch(
    result: tuple[object, object, object],
) -> None:
    with pytest.raises(ValueError):
        ProviderUsageResult(*result)  # type: ignore[arg-type]
