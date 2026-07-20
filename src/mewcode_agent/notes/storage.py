"""Strict Markdown parsing and atomic storage for user/project notes."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from mewcode_agent.notes.models import NotePaths, NoteScope, NotesError, NotesSnapshot

NOTES_FILE_BYTES = 256 * 1024

_USER_TITLE = "# MewCode User Notes"
_PROJECT_TITLE = "# MewCode Project Notes"
_USER_SECTIONS = ("## 用户偏好", "## 纠正反馈")
_PROJECT_SECTIONS = ("## 项目知识", "## 参考资料")
_BINARY_FLAG = getattr(os, "O_BINARY", 0)


def note_paths(*, user_root: Path, project_root: Path) -> NotePaths:
    try:
        normalized_user_root = user_root.resolve(strict=False)
        normalized_project_root = project_root.resolve(strict=True)
        return NotePaths(
            normalized_user_root / "notes.md",
            normalized_project_root / ".mewcode" / "notes.md",
        )
    except OSError as exc:
        raise NotesError("notes_read_failed") from exc


def _parse_entries(
    lines: list[str],
    *,
    index: int,
    next_heading: str | None,
) -> tuple[tuple[str, ...], int]:
    if index >= len(lines):
        if next_heading is None:
            return (), index
        raise NotesError("notes_invalid_format")
    if lines[index] != "":
        raise NotesError("notes_invalid_format")
    index += 1
    entries: list[str] = []
    while index < len(lines) and lines[index].startswith("- "):
        entry = lines[index][2:]
        if (
            not entry.strip()
            or "\0" in entry
            or len(entry) > 1000
            or len(entries) >= 128
        ):
            raise NotesError("notes_invalid_format")
        entries.append(entry)
        index += 1
    if next_heading is None:
        if index != len(lines):
            raise NotesError("notes_invalid_format")
        return tuple(entries), index
    if entries:
        if index >= len(lines) or lines[index] != "":
            raise NotesError("notes_invalid_format")
        index += 1
    if index >= len(lines) or lines[index] != next_heading:
        raise NotesError("notes_invalid_format")
    return tuple(entries), index + 1


def _parse_markdown(
    content: str,
    *,
    title: str,
    sections: tuple[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    if normalized.endswith("\n") or "\0" in normalized:
        raise NotesError("notes_invalid_format")
    lines = normalized.split("\n")
    if len(lines) < 3 or lines[:3] != [title, "", sections[0]]:
        raise NotesError("notes_invalid_format")
    first, index = _parse_entries(
        lines,
        index=3,
        next_heading=sections[1],
    )
    second, index = _parse_entries(
        lines,
        index=index,
        next_heading=None,
    )
    if index != len(lines):
        raise NotesError("notes_invalid_format")
    return first, second


def _read_layer(
    path: Path,
    *,
    scope: NoteScope,
    title: str,
    sections: tuple[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        present = path.exists() or path.is_symlink()
    except OSError as exc:
        raise NotesError("notes_read_failed") from exc
    if not present:
        return (), ()
    try:
        allowed_root = path.parent if scope == "user" else path.parent.parent
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(allowed_root)
        if path.is_symlink() or not path.is_file():
            raise NotesError("notes_read_failed")
        with path.open("rb") as stream:
            payload = stream.read(NOTES_FILE_BYTES + 1)
    except NotesError:
        raise
    except (OSError, ValueError) as exc:
        raise NotesError("notes_read_failed") from exc
    if len(payload) > NOTES_FILE_BYTES:
        raise NotesError("notes_file_too_large")
    try:
        content = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise NotesError("notes_invalid_format") from exc
    return _parse_markdown(content, title=title, sections=sections)


def load_notes(*, paths: NotePaths) -> NotesSnapshot:
    user_preferences, correction_feedback = _read_layer(
        paths.user,
        scope="user",
        title=_USER_TITLE,
        sections=_USER_SECTIONS,
    )
    project_knowledge, references = _read_layer(
        paths.project,
        scope="project",
        title=_PROJECT_TITLE,
        sections=_PROJECT_SECTIONS,
    )
    return NotesSnapshot(
        user_preferences,
        correction_feedback,
        project_knowledge,
        references,
    )


def _render_layer(
    *,
    title: str,
    sections: tuple[str, str],
    first: tuple[str, ...],
    second: tuple[str, ...],
) -> str:
    lines = [title, "", sections[0], ""]
    lines.extend(f"- {entry}" for entry in first)
    if first:
        lines.append("")
    lines.append(sections[1])
    if second:
        lines.append("")
        lines.extend(f"- {entry}" for entry in second)
    return "\n".join(lines) + "\n"


def render_user_notes(snapshot: NotesSnapshot) -> str:
    return _render_layer(
        title=_USER_TITLE,
        sections=_USER_SECTIONS,
        first=snapshot.user_preferences,
        second=snapshot.correction_feedback,
    )


def render_project_notes(snapshot: NotesSnapshot) -> str:
    return _render_layer(
        title=_PROJECT_TITLE,
        sections=_PROJECT_SECTIONS,
        first=snapshot.project_knowledge,
        second=snapshot.references,
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("write returned no progress")
        offset += written


def _atomic_write(path: Path, content: str, *, scope: NoteScope) -> None:
    payload = content.encode("utf-8")
    if len(payload) > NOTES_FILE_BYTES:
        raise NotesError("notes_file_too_large")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        allowed_root = path.parent if scope == "user" else path.parent.parent
        normalized_target = path.resolve(strict=False)
        normalized_target.relative_to(allowed_root)
        parent_existed = path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt" and not parent_existed:
            path.parent.chmod(0o700)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY_FLAG,
            0o600,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except NotesError:
        raise
    except (OSError, ValueError) as exc:
        raise NotesError("notes_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_note_scope(
    *,
    paths: NotePaths,
    scope: NoteScope,
    snapshot: NotesSnapshot,
) -> None:
    if scope == "user":
        _atomic_write(paths.user, render_user_notes(snapshot), scope="user")
    elif scope == "project":
        _atomic_write(
            paths.project,
            render_project_notes(snapshot),
            scope="project",
        )
    else:
        raise ValueError("scope 必须为 user 或 project")
