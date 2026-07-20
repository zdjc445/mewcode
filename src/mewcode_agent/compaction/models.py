"""Validated models and stable errors for context compaction."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


KIB = 1024
MIB = 1024 * KIB


class ContextCompactionError(RuntimeError):
    """A sanitized context-compaction failure safe for the UI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    single_tool_result_bytes: int = 64 * KIB
    tool_batch_bytes: int = 128 * KIB
    preview_bytes: int = 8 * KIB
    preview_head_bytes: int = 6 * KIB
    preview_tail_bytes: int = 2 * KIB
    artifact_bytes: int = 64 * MIB
    artifact_session_bytes: int = 512 * MIB
    artifact_read_characters: int = 8192
    auto_trigger_ratio: float = 0.80
    target_ratio: float = 0.60
    summary_response_bytes: int = 64 * KIB
    max_summary_failures: int = 3
    manual_retained_units: int = 4
    stale_artifact_seconds: int = 24 * 60 * 60
    framing_safety_tokens: int = 4096

    def __post_init__(self) -> None:
        integer_values = (
            self.single_tool_result_bytes,
            self.tool_batch_bytes,
            self.preview_bytes,
            self.preview_head_bytes,
            self.preview_tail_bytes,
            self.artifact_bytes,
            self.artifact_session_bytes,
            self.artifact_read_characters,
            self.summary_response_bytes,
            self.max_summary_failures,
            self.manual_retained_units,
            self.stale_artifact_seconds,
            self.framing_safety_tokens,
        )
        if any(type(value) is not int or value <= 0 for value in integer_values):
            raise ValueError("压缩配置整数值必须大于 0")
        if self.preview_head_bytes + self.preview_tail_bytes != self.preview_bytes:
            raise ValueError("预览头尾预算必须等于预览总预算")
        if self.single_tool_result_bytes > self.tool_batch_bytes:
            raise ValueError("单结果上限不能大于批次上限")
        if self.preview_bytes >= self.single_tool_result_bytes:
            raise ValueError("预览上限必须小于单结果上限")
        if self.artifact_bytes > self.artifact_session_bytes:
            raise ValueError("单 artifact 上限不能大于会话上限")
        if not 0 < self.target_ratio < self.auto_trigger_ratio < 1:
            raise ValueError("摘要目标比例必须小于触发比例且都位于 0 与 1 之间")


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    path: Path
    sha256: str
    utf8_bytes: int

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("artifact path 必须是绝对路径")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("artifact sha256 必须是 64 位小写十六进制")
        if type(self.utf8_bytes) is not int or self.utf8_bytes < 0:
            raise ValueError("artifact utf8_bytes 必须是非负整数")


@dataclass(frozen=True, slots=True)
class ToolCompactionResult:
    processed_batches: int
    externalized_results: int
    original_inline_bytes: int
    compacted_inline_bytes: int

    def __post_init__(self) -> None:
        values = (
            self.processed_batches,
            self.externalized_results,
            self.original_inline_bytes,
            self.compacted_inline_bytes,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("工具压缩统计必须是非负整数")


def _validate_string_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"{field_name} 必须是字符串 tuple")


@dataclass(frozen=True, slots=True)
class SummarySections:
    primary_requests: tuple[str, ...]
    key_concepts: tuple[str, ...]
    files_and_code: tuple[str, ...]
    errors_and_fixes: tuple[str, ...]
    solution_process: tuple[str, ...]
    pending_tasks: tuple[str, ...]
    current_work: tuple[str, ...]
    next_step: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "primary_requests",
            "key_concepts",
            "files_and_code",
            "errors_and_fixes",
            "solution_process",
            "pending_tasks",
            "current_work",
            "next_step",
        ):
            _validate_string_tuple(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "primary_requests": list(self.primary_requests),
            "key_concepts": list(self.key_concepts),
            "files_and_code": list(self.files_and_code),
            "errors_and_fixes": list(self.errors_and_fixes),
            "solution_process": list(self.solution_process),
            "pending_tasks": list(self.pending_tasks),
            "current_work": list(self.current_work),
            "next_step": list(self.next_step),
        }


@dataclass(frozen=True, slots=True)
class VerbatimUserMessage:
    history_index: int
    content: str

    def __post_init__(self) -> None:
        if type(self.history_index) is not int or self.history_index < 0:
            raise ValueError("history_index 必须是非负整数")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("verbatim user content 必须是非空字符串")

    def to_dict(self) -> dict[str, object]:
        return {
            "history_index": self.history_index,
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class SummaryCheckpoint:
    generation: int
    covered_history_end: int
    sections: SummarySections
    user_messages_verbatim: tuple[VerbatimUserMessage, ...]

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("summary generation 必须大于 0")
        if (
            type(self.covered_history_end) is not int
            or self.covered_history_end <= 0
        ):
            raise ValueError("covered_history_end 必须大于 0")
        if not isinstance(self.sections, SummarySections):
            raise ValueError("sections 类型无效")
        if not isinstance(self.user_messages_verbatim, tuple):
            raise ValueError("user_messages_verbatim 必须是 tuple")
        indexes = tuple(
            message.history_index for message in self.user_messages_verbatim
        )
        if indexes != tuple(sorted(indexes)) or len(indexes) != len(set(indexes)):
            raise ValueError("用户原话 history_index 必须严格递增")
        if indexes and indexes[-1] >= self.covered_history_end:
            raise ValueError("用户原话索引必须位于 checkpoint 覆盖范围")

    def to_dict(self) -> dict[str, object]:
        sections = self.sections.to_dict()
        return {
            "schema_version": 1,
            "generation": self.generation,
            "covered_history_end": self.covered_history_end,
            "primary_requests": sections["primary_requests"],
            "key_concepts": sections["key_concepts"],
            "files_and_code": sections["files_and_code"],
            "errors_and_fixes": sections["errors_and_fixes"],
            "solution_process": sections["solution_process"],
            "user_messages_verbatim": [
                message.to_dict()
                for message in self.user_messages_verbatim
            ],
            "pending_tasks": sections["pending_tasks"],
            "current_work": sections["current_work"],
            "next_step": sections["next_step"],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class ContextEstimate:
    estimated_prompt_tokens: int
    used_actual_baseline: bool

    def __post_init__(self) -> None:
        if (
            type(self.estimated_prompt_tokens) is not int
            or self.estimated_prompt_tokens < 0
        ):
            raise ValueError("estimated_prompt_tokens 必须是非负整数")
        if type(self.used_actual_baseline) is not bool:
            raise ValueError("used_actual_baseline 必须是布尔值")
