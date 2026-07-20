"""Context pressure estimates calibrated by real provider usage."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from mewcode_agent.compaction.models import CompactionConfig, ContextEstimate
from mewcode_agent.providers.base import (
    LLMProvider,
    ProviderRequest,
    ProviderUsageResult,
)


@dataclass(frozen=True, slots=True)
class _RequestShape:
    static_json: str
    message_json: tuple[str, ...]
    full_utf8_bytes: int


class ContextTokenEstimator:
    """Use actual prompt usage plus deterministic UTF-8 growth estimates."""

    def __init__(self, *, config: CompactionConfig | None = None) -> None:
        self._config = config or CompactionConfig()
        self._last_shape: _RequestShape | None = None
        self._last_actual_prompt_tokens: int | None = None

    def estimate(
        self,
        provider: LLMProvider,
        request: ProviderRequest,
    ) -> ContextEstimate:
        shape = self._shape(provider.prompt_payload(request))
        previous = self._last_shape
        actual = self._last_actual_prompt_tokens
        if (
            previous is not None
            and actual is not None
            and shape.static_json == previous.static_json
            and len(shape.message_json) >= len(previous.message_json)
            and shape.message_json[: len(previous.message_json)]
            == previous.message_json
        ):
            appended = shape.message_json[len(previous.message_json) :]
            appended_bytes = sum(
                len(message.encode("utf-8")) for message in appended
            )
            return ContextEstimate(
                actual
                + appended_bytes
                + self._config.framing_safety_tokens,
                True,
            )
        return ContextEstimate(
            shape.full_utf8_bytes + self._config.framing_safety_tokens,
            False,
        )

    def record_usage(
        self,
        provider: LLMProvider,
        request: ProviderRequest,
        result: ProviderUsageResult,
    ) -> None:
        if result.status != "available" or result.usage is None:
            return
        self._last_shape = self._shape(provider.prompt_payload(request))
        self._last_actual_prompt_tokens = result.usage.prompt_tokens

    def reset_session(self) -> None:
        self._last_shape = None
        self._last_actual_prompt_tokens = None

    @staticmethod
    def _shape(payload: dict[str, Any]) -> _RequestShape:
        if not isinstance(payload, dict):
            raise ValueError("provider prompt_payload 必须是 object")
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise ValueError("provider prompt_payload.messages 必须是 list")
        static_payload = {
            key: value for key, value in payload.items() if key != "messages"
        }
        static_json = json.dumps(
            static_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        message_json = tuple(
            json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for message in messages
        )
        full_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return _RequestShape(
            static_json,
            message_json,
            len(full_json.encode("utf-8")),
        )
