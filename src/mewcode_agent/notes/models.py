"""Validated note data and safe failures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from mewcode_agent.prompting.models import RuntimeInstruction

NoteScope: TypeAlias = Literal["user", "project"]
NoteErrorCode: TypeAlias = Literal[
    "notes_read_failed",
    "notes_invalid_format",
    "notes_file_too_large",
    "notes_write_failed",
    "notes_update_failed",
    "notes_update_invalid",
    "notes_tool_call_forbidden",
]
_ERROR_MESSAGES: dict[NoteErrorCode, str] = {
    "notes_read_failed": "笔记文件无法读取",
    "notes_invalid_format": "笔记 Markdown 结构无效",
    "notes_file_too_large": "笔记文件超过 256 KiB",
    "notes_write_failed": "笔记文件写入失败",
    "notes_update_failed": "笔记模型调用失败",
    "notes_update_invalid": "笔记模型响应无效",
    "notes_tool_call_forbidden": "笔记模型返回了禁止的工具调用",
}


class NotesError(RuntimeError):
    """A stable notes failure safe to display in the UI."""

    def __init__(self, code: NoteErrorCode) -> None:
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        super().__init__(f"{code}: {self.message}")


def _validate_entries(entries: tuple[str, ...], field_name: str) -> None:
    if not isinstance(entries, tuple):
        raise ValueError(f"{field_name} 必须是 tuple")
    if len(entries) > 128:
        raise ValueError(f"{field_name} 最多包含 128 条")
    for entry in entries:
        if (
            not isinstance(entry, str)
            or not entry.strip()
            or "\n" in entry
            or "\r" in entry
            or "\0" in entry
            or len(entry) > 1000
        ):
            raise ValueError(f"{field_name} 包含无效条目")


@dataclass(frozen=True, slots=True)
class NotesSnapshot:
    user_preferences: tuple[str, ...] = ()
    correction_feedback: tuple[str, ...] = ()
    project_knowledge: tuple[str, ...] = ()
    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "user_preferences",
            "correction_feedback",
            "project_knowledge",
            "references",
        ):
            _validate_entries(getattr(self, field_name), field_name)

    def user_is_empty(self) -> bool:
        return not self.user_preferences and not self.correction_feedback

    def project_is_empty(self) -> bool:
        return not self.project_knowledge and not self.references

    def runtime_controls(
        self,
        *,
        generation: int,
        include_empty: tuple[NoteScope, ...] = (),
    ) -> tuple[RuntimeInstruction, ...]:
        if type(generation) is not int or generation <= 0:
            raise ValueError("generation 必须是大于 0 的整数")
        controls: list[RuntimeInstruction] = []
        if not self.project_is_empty() or "project" in include_empty:
            controls.append(self.runtime_control("project", generation))
        if not self.user_is_empty() or "user" in include_empty:
            controls.append(self.runtime_control("user", generation))
        return tuple(controls)

    def runtime_control(
        self,
        scope: NoteScope,
        generation: int,
    ) -> RuntimeInstruction:
        if scope not in ("project", "user"):
            raise ValueError("scope 必须为 project 或 user")
        if type(generation) is not int or generation <= 0:
            raise ValueError("generation 必须是大于 0 的整数")
        import json

        data = (
            {
                "project_knowledge": list(self.project_knowledge),
                "references": list(self.references),
            }
            if scope == "project"
            else {
                "user_preferences": list(self.user_preferences),
                "correction_feedback": list(self.correction_feedback),
            }
        )
        return RuntimeInstruction(
            f"runtime.notes.{scope}.generation_{generation}",
            "context",
            "session",
            json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "notes",
        )


@dataclass(frozen=True, slots=True)
class NotePaths:
    user: Path
    project: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.user, Path)
            or not self.user.is_absolute()
            or not isinstance(self.project, Path)
            or not self.project.is_absolute()
        ):
            raise ValueError("笔记路径必须是绝对 Path")


@dataclass(frozen=True, slots=True)
class NoteClearTarget:
    scope: NoteScope
    path: Path

    def __post_init__(self) -> None:
        if self.scope not in ("user", "project"):
            raise ValueError("scope 必须为 user 或 project")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("path 必须是绝对 Path")


@dataclass(frozen=True, slots=True)
class NoteWarning:
    scope: NoteScope | None
    code: NoteErrorCode

    def __post_init__(self) -> None:
        if self.scope not in (None, "user", "project"):
            raise ValueError("scope 必须为 user、project 或 None")
        if self.code not in _ERROR_MESSAGES:
            raise ValueError("note warning code 无效")
