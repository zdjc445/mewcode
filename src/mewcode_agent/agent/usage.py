"""Optional cache-evaluation usage collection."""

from dataclasses import dataclass
from typing import Literal, Protocol

from mewcode_agent.agent.events import AgentRunMode
from mewcode_agent.providers.base import ProviderUsageResult


@dataclass(frozen=True, slots=True)
class UsageRecord:
    provider_id: str
    request_sequence: int
    round_number: int
    mode: AgentRunMode
    result: ProviderUsageResult

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ValueError("provider_id 必须为非空字符串")
        if (
            type(self.request_sequence) is not int
            or self.request_sequence <= 0
        ):
            raise ValueError("request_sequence 必须大于 0")
        if type(self.round_number) is not int or self.round_number <= 0:
            raise ValueError("round_number 必须大于 0")
        if self.mode not in ("planning", "executing"):
            raise ValueError("mode 必须为 planning 或 executing")

    @property
    def request_kind(self) -> Literal["agent"]:
        return "agent"


@dataclass(frozen=True, slots=True)
class CompactionUsageRecord:
    provider_id: str
    generation: int
    result: ProviderUsageResult

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ValueError("provider_id 必须为非空字符串")
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("generation 必须大于 0")

    @property
    def request_kind(self) -> Literal["compaction"]:
        return "compaction"


@dataclass(frozen=True, slots=True)
class NoteUsageRecord:
    provider_id: str
    generation: int
    result: ProviderUsageResult

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ValueError("provider_id 必须为非空字符串")
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("generation 必须大于 0")

    @property
    def request_kind(self) -> Literal["notes"]:
        return "notes"


class UsageCollector(Protocol):
    def record(
        self,
        record: UsageRecord | CompactionUsageRecord | NoteUsageRecord,
    ) -> None: ...
