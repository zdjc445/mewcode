"""Strict, sandboxed loading for project and user instruction files."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from mewcode_agent.instructions.models import (
    InstructionConfigError,
    InstructionDocument,
    InstructionErrorCode,
    InstructionLayer,
)

INSTRUCTION_MAX_INCLUDE_DEPTH = 5
INSTRUCTION_FILE_BYTES = 64 * 1024
INSTRUCTION_TOTAL_BYTES = 256 * 1024

_INCLUDE_DIRECTIVE = re.compile(r"@include[ \t]+<([^<>]*)>\Z")


def _raise_error(
    code: InstructionErrorCode,
    *,
    layer: InstructionLayer,
    relative_path: str,
    cause: BaseException | None = None,
) -> None:
    error = InstructionConfigError(
        code,
        layer=layer,
        relative_path=relative_path,
    )
    if cause is None:
        raise error
    raise error from cause


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _relative_display(path: Path, root: Path, fallback: str) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return fallback


def _read_bytes(
    path: Path,
    *,
    layer: InstructionLayer,
    relative_path: str,
    include: bool,
) -> bytes:
    try:
        if not path.is_file():
            _raise_error(
                (
                    "instruction_include_not_found"
                    if include
                    else "instruction_read_failed"
                ),
                layer=layer,
                relative_path=relative_path,
            )
        with path.open("rb") as stream:
            payload = stream.read(INSTRUCTION_FILE_BYTES + 1)
    except InstructionConfigError:
        raise
    except OSError as exc:
        _raise_error(
            "instruction_read_failed",
            layer=layer,
            relative_path=relative_path,
            cause=exc,
        )
    if len(payload) > INSTRUCTION_FILE_BYTES:
        _raise_error(
            "instruction_file_too_large",
            layer=layer,
            relative_path=relative_path,
        )
    return payload


def _decode(
    payload: bytes,
    *,
    layer: InstructionLayer,
    relative_path: str,
) -> str:
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        _raise_error(
            "instruction_invalid_utf8",
            layer=layer,
            relative_path=relative_path,
            cause=exc,
        )


def _validate_include_path(
    raw_path: str,
    *,
    layer: InstructionLayer,
    source_relative_path: str,
) -> str:
    include_path = raw_path.strip(" \t")
    if not include_path or "\0" in include_path:
        _raise_error(
            "instruction_include_invalid",
            layer=layer,
            relative_path=source_relative_path,
        )
    windows_path = PureWindowsPath(include_path)
    posix_path = PurePosixPath(include_path)
    if (
        windows_path.is_absolute()
        or bool(windows_path.drive)
        or posix_path.is_absolute()
    ):
        _raise_error(
            "instruction_include_outside_root",
            layer=layer,
            relative_path=source_relative_path,
        )
    return include_path


def _resolve_include(
    include_path: str,
    *,
    current_path: Path,
    root: Path,
    layer: InstructionLayer,
    source_relative_path: str,
) -> Path:
    candidate = current_path.parent / include_path
    try:
        normalized_candidate = candidate.resolve(strict=False)
    except OSError as exc:
        _raise_error(
            "instruction_read_failed",
            layer=layer,
            relative_path=source_relative_path,
            cause=exc,
        )
    if not _is_within(normalized_candidate, root):
        _raise_error(
            "instruction_include_outside_root",
            layer=layer,
            relative_path=source_relative_path,
        )
    target_relative_path = _relative_display(
        normalized_candidate,
        root,
        source_relative_path,
    )
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        _raise_error(
            "instruction_include_not_found",
            layer=layer,
            relative_path=target_relative_path,
            cause=exc,
        )
    except OSError as exc:
        _raise_error(
            "instruction_read_failed",
            layer=layer,
            relative_path=source_relative_path,
            cause=exc,
        )
    if not _is_within(resolved, root):
        _raise_error(
            "instruction_include_outside_root",
            layer=layer,
            relative_path=source_relative_path,
        )
    return resolved


def _expand_file(
    path: Path,
    *,
    root: Path,
    layer: InstructionLayer,
    relative_path: str,
    depth: int,
    stack: tuple[Path, ...],
    include: bool,
) -> str:
    if depth > INSTRUCTION_MAX_INCLUDE_DEPTH:
        _raise_error(
            "instruction_include_depth_exceeded",
            layer=layer,
            relative_path=relative_path,
        )
    if path in stack:
        _raise_error(
            "instruction_include_cycle",
            layer=layer,
            relative_path=relative_path,
        )
    payload = _read_bytes(
        path,
        layer=layer,
        relative_path=relative_path,
        include=include,
    )
    content = _decode(
        payload,
        layer=layer,
        relative_path=relative_path,
    )
    pieces: list[str] = []
    expanded_bytes = 0
    trailing_newlines = 0

    def append_piece(piece: str) -> None:
        nonlocal expanded_bytes, trailing_newlines
        expanded_bytes += len(piece.encode("utf-8"))
        without_trailing = piece.rstrip("\n")
        if without_trailing:
            trailing_newlines = len(piece) - len(without_trailing)
        else:
            trailing_newlines += len(piece)
        normalized_bytes = expanded_bytes - max(trailing_newlines - 1, 0)
        if normalized_bytes > INSTRUCTION_TOTAL_BYTES:
            _raise_error(
                "instruction_total_too_large",
                layer=layer,
                relative_path=relative_path,
            )
        pieces.append(piece)

    normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized_content.splitlines(keepends=True):
        line_body = line.rstrip("\r\n")
        stripped = line_body.strip(" \t")
        match = _INCLUDE_DIRECTIVE.fullmatch(stripped)
        if match is None:
            if stripped.startswith("@include"):
                _raise_error(
                    "instruction_include_invalid",
                    layer=layer,
                    relative_path=relative_path,
                )
            append_piece(line)
            continue
        child_raw_path = _validate_include_path(
            match.group(1),
            layer=layer,
            source_relative_path=relative_path,
        )
        child_path = _resolve_include(
            child_raw_path,
            current_path=path,
            root=root,
            layer=layer,
            source_relative_path=relative_path,
        )
        child_relative_path = _relative_display(
            child_path,
            root,
            relative_path,
        )
        append_piece(
            _expand_file(
                child_path,
                root=root,
                layer=layer,
                relative_path=child_relative_path,
                depth=depth + 1,
                stack=(*stack, path),
                include=True,
            )
        )
    expanded = "".join(pieces).rstrip("\r\n") + "\n"
    return expanded


def _load_layer(
    *,
    root: Path,
    entry_name: str,
    layer: InstructionLayer,
) -> InstructionDocument | None:
    try:
        normalized_root = root.resolve(strict=False)
    except OSError as exc:
        _raise_error(
            "instruction_read_failed",
            layer=layer,
            relative_path=entry_name,
            cause=exc,
        )
    entry = root / entry_name
    try:
        present = entry.exists() or entry.is_symlink()
    except OSError as exc:
        _raise_error(
            "instruction_read_failed",
            layer=layer,
            relative_path=entry_name,
            cause=exc,
        )
    if not present:
        return None
    try:
        resolved_entry = entry.resolve(strict=True)
    except OSError as exc:
        _raise_error(
            "instruction_read_failed",
            layer=layer,
            relative_path=entry_name,
            cause=exc,
        )
    if not _is_within(resolved_entry, normalized_root):
        _raise_error(
            "instruction_include_outside_root",
            layer=layer,
            relative_path=entry_name,
        )
    content = _expand_file(
        resolved_entry,
        root=normalized_root,
        layer=layer,
        relative_path=entry_name,
        depth=0,
        stack=(),
        include=False,
    )
    if not content.strip():
        return None
    return InstructionDocument(layer, entry_name, content)


def load_instruction_documents(
    *,
    user_root: Path,
    project_root: Path,
) -> tuple[InstructionDocument, ...]:
    """Load project then user instructions from their exact entry paths."""

    documents: list[InstructionDocument] = []
    for root, entry_name, layer in (
        (project_root, "MEWCODE.md", "project"),
        (user_root, "INSTRUCTIONS.md", "user"),
    ):
        document = _load_layer(
            root=root,
            entry_name=entry_name,
            layer=layer,
        )
        if document is not None:
            documents.append(document)
    return tuple(documents)
