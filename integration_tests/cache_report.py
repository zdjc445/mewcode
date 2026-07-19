"""Sensitive-content-free cache evaluation report writing."""

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path

from mewcode_agent.agent.usage import UsageRecord


@dataclass(frozen=True, slots=True)
class CacheScenario:
    scenario_id: str
    provider_id: str
    attempts: tuple[tuple[int, UsageRecord], ...]

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise ValueError("scenario_id 必须为非空字符串")
        if not self.provider_id.strip():
            raise ValueError("provider_id 必须为非空字符串")
        if not self.attempts:
            raise ValueError("attempts 不能为空")
        seen: set[int] = set()
        for attempt, record in self.attempts:
            if type(attempt) is not int or attempt <= 0:
                raise ValueError("attempt 必须为大于 0 的整数")
            if attempt in seen:
                raise ValueError("attempt 不能重复")
            if record.provider_id != self.provider_id:
                raise ValueError("record.provider_id 与 scenario 不一致")
            seen.add(attempt)


def _attempt(attempt: int, record: UsageRecord) -> dict[str, object]:
    if record.mode not in ("planning", "executing"):
        raise ValueError("mode 必须为 planning 或 executing")
    result = record.result
    usage = result.usage
    if usage is None:
        prompt_tokens = None
        cache_hit_tokens = None
        cache_miss_tokens = None
        completion_tokens = None
        cache_hit_rate = None
    else:
        prompt_tokens = usage.prompt_tokens
        cache_hit_tokens = usage.cache_hit_tokens
        cache_miss_tokens = usage.cache_miss_tokens
        completion_tokens = usage.completion_tokens
        cache_hit_rate = (
            usage.cache_hit_tokens / usage.prompt_tokens
            if usage.prompt_tokens > 0
            else None
        )
    return {
        "attempt": attempt,
        "request_sequence": record.request_sequence,
        "round_number": record.round_number,
        "mode": record.mode,
        "status": result.status,
        "prompt_tokens": prompt_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_rate": cache_hit_rate,
        "reason": result.reason,
    }


def write_cache_report(
    path: Path,
    *,
    model: str,
    scenarios: tuple[CacheScenario, ...],
    generated_at: datetime,
) -> None:
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model 必须为非空字符串")
    if generated_at.utcoffset() is None:
        raise ValueError("generated_at 必须包含 UTC offset")
    payload = {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "model": model,
        "scenarios": [
            {
                "scenario_id": scenario.scenario_id,
                "provider_id": scenario.provider_id,
                "attempts": [
                    _attempt(attempt, record)
                    for attempt, record in scenario.attempts
                ],
            }
            for scenario in scenarios
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
