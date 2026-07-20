"""Validated models and safe errors for hand-written instructions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from mewcode_agent.prompting.models import RuntimeInstruction

InstructionLayer: TypeAlias = Literal["project", "user"]
InstructionErrorCode: TypeAlias = Literal[
    "instruction_read_failed",
    "instruction_invalid_utf8",
    "instruction_file_too_large",
    "instruction_total_too_large",
    "instruction_include_invalid",
    "instruction_include_not_found",
    "instruction_include_outside_root",
    "instruction_include_cycle",
    "instruction_include_depth_exceeded",
]

_ERROR_MESSAGES: dict[InstructionErrorCode, str] = {
    "instruction_read_failed": "指令文件无法读取",
    "instruction_invalid_utf8": "指令文件不是有效 UTF-8",
    "instruction_file_too_large": "指令文件超过 64 KiB",
    "instruction_total_too_large": "指令层展开结果超过 256 KiB",
    "instruction_include_invalid": "指令 include 语法或路径无效",
    "instruction_include_not_found": "指令 include 目标不存在或不是普通文件",
    "instruction_include_outside_root": "指令 include 超出所属根目录",
    "instruction_include_cycle": "指令 include 出现循环",
    "instruction_include_depth_exceeded": "指令 include 深度超过 5",
}


class InstructionConfigError(RuntimeError):
    """A stable, content-free startup error for instruction loading."""

    def __init__(
        self,
        code: InstructionErrorCode,
        *,
        layer: InstructionLayer,
        relative_path: str,
    ) -> None:
        self.code = code
        self.layer = layer
        self.relative_path = relative_path
        super().__init__(
            f"{code}: {_ERROR_MESSAGES[code]} "
            f"(layer={layer}, path={relative_path})"
        )


@dataclass(frozen=True, slots=True)
class InstructionDocument:
    layer: InstructionLayer
    relative_path: str
    content: str

    def __post_init__(self) -> None:
        if self.layer not in ("project", "user"):
            raise ValueError("layer 必须为 project 或 user")
        if not isinstance(self.relative_path, str) or not self.relative_path:
            raise ValueError("relative_path 必须为非空字符串")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("content 必须为非空字符串")

    def to_runtime_instruction(self) -> RuntimeInstruction:
        instruction_id = {
            "project": "runtime.instructions.project",
            "user": "runtime.instructions.user",
        }[self.layer]
        return RuntimeInstruction(
            instruction_id,
            "instruction",
            "session",
            self.content,
            self.layer,
        )
