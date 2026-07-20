"""Validated models and stable errors for context compaction."""

from __future__ import annotations

from dataclasses import dataclass
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
