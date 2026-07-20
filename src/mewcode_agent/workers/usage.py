"""Per-worker usage accounting isolated from the main collector."""

from __future__ import annotations

from mewcode_agent.agent.usage import (
    CompactionUsageRecord,
    NoteUsageRecord,
    UsageRecord,
)
from mewcode_agent.workers.models import WorkerUsageSnapshot


class WorkerUsageCollector:
    def __init__(self) -> None:
        self._prompt_tokens = 0
        self._cache_hit_tokens = 0
        self._cache_miss_tokens = 0
        self._completion_tokens = 0
        self._unavailable_rounds = 0

    def record(
        self,
        record: UsageRecord | CompactionUsageRecord | NoteUsageRecord,
    ) -> None:
        if not isinstance(record, UsageRecord):
            return
        result = record.result
        if result.status != "available":
            self._unavailable_rounds += 1
            return
        assert result.usage is not None
        self._prompt_tokens += result.usage.prompt_tokens
        self._cache_hit_tokens += result.usage.cache_hit_tokens
        self._cache_miss_tokens += result.usage.cache_miss_tokens
        self._completion_tokens += result.usage.completion_tokens

    def snapshot(self) -> WorkerUsageSnapshot:
        return WorkerUsageSnapshot(
            self._prompt_tokens,
            self._cache_hit_tokens,
            self._cache_miss_tokens,
            self._completion_tokens,
            self._unavailable_rounds,
        )
