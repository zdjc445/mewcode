"""Durable JSONL append, metadata updates, and exceptional recovery."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
from typing import Callable
from uuid import uuid4

from mewcode_agent.models import ChatMessage
from mewcode_agent.sessions.models import (
    SESSION_RECORD_BYTES,
    SessionDiagnostic,
    SessionError,
    SessionMeta,
    SessionRecord,
    SessionRecovery,
    validate_session_id,
)

_META_FILENAME = "meta.json"
_MESSAGES_FILENAME = "messages.jsonl"
_BINARY_FLAG = getattr(os, "O_BINARY", 0)


class _DuplicateKeyError(ValueError):
    pass


def _pairs_to_dict(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _loads(payload: bytes) -> object:
    text = payload.decode("utf-8", errors="strict")
    return json.loads(text, object_pairs_hook=_pairs_to_dict)


def _timestamp(now_factory: Callable[[], datetime]) -> str:
    current = now_factory()
    if not isinstance(current, datetime) or current.utcoffset() is None:
        raise ValueError("当前时间必须包含 UTC offset")
    return current.isoformat()


def _json_bytes(value: object, *, newline: bool) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return encoded + (b"\n" if newline else b"")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("write returned no progress")
        offset += written


def _set_private_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)


def _set_private_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY_FLAG,
            0o600,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        _set_private_file(temporary)
        os.replace(temporary, path)
        _set_private_file(path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _append_line(path: Path, payload: bytes) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise OSError("messages path is not a regular file")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | _BINARY_FLAG,
        0o600,
    )
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _set_private_file(path)


def _title(messages: tuple[ChatMessage, ...]) -> str:
    for message in messages:
        if message.role != "user":
            continue
        for line in message.content.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:80]
    return "新会话"


def _summary(messages: tuple[ChatMessage, ...]) -> str:
    for message in reversed(messages):
        if message.role == "assistant" and not message.tool_calls:
            return re.sub(r"\s+", " ", message.content).strip()[:200]
    return ""


def _build_meta(
    *,
    session_id: str,
    project_root: str,
    provider_id: str,
    model: str,
    records: tuple[SessionRecord, ...],
    fallback_timestamp: str,
) -> SessionMeta:
    messages = tuple(record.message for record in records)
    created_at = records[0].created_at if records else fallback_timestamp
    updated_at = records[-1].created_at if records else fallback_timestamp
    return SessionMeta(
        session_id=session_id,
        project_root=project_root,
        provider_id=provider_id,
        model=model,
        title=_title(messages),
        summary=_summary(messages),
        message_count=len(records),
        last_sequence=records[-1].sequence if records else 0,
        created_at=created_at,
        updated_at=updated_at,
    )


def _meta_matches_records(
    meta: SessionMeta,
    records: tuple[SessionRecord, ...],
) -> bool:
    messages = tuple(record.message for record in records)
    if not records:
        return (
            meta.message_count == 0
            and meta.last_sequence == 0
            and meta.title == "新会话"
            and meta.summary == ""
        )
    return (
        meta.message_count == len(records)
        and meta.last_sequence == records[-1].sequence
        and meta.created_at == records[0].created_at
        and meta.updated_at == records[-1].created_at
        and meta.title == _title(messages)
        and meta.summary == _summary(messages)
    )


def load_session_meta(
    path: Path,
    *,
    expected_session_id: str | None = None,
) -> SessionMeta:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("meta is not a regular file")
        with path.open("rb") as stream:
            payload = stream.read(SESSION_RECORD_BYTES + 1)
        if len(payload) > SESSION_RECORD_BYTES:
            raise ValueError("meta is too large")
        meta = SessionMeta.from_dict(_loads(payload))
        if (
            expected_session_id is not None
            and meta.session_id != expected_session_id
        ):
            raise ValueError("meta session id mismatch")
        return meta
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionError("session_invalid_meta") from exc


def _session_directory(sessions_root: Path, session_id: str) -> Path:
    try:
        validate_session_id(session_id)
    except ValueError as exc:
        raise SessionError("session_not_found") from exc
    try:
        root = sessions_root.resolve(strict=False)
        directory = root / session_id
        if not directory.exists():
            raise SessionError("session_not_found")
        if directory.is_symlink() or not directory.is_dir():
            raise SessionError("session_access_denied")
        if directory.resolve(strict=True).parent != root:
            raise SessionError("session_access_denied")
        return directory
    except SessionError:
        raise
    except OSError as exc:
        raise SessionError("session_access_denied") from exc


def _scan_records(
    messages_path: Path,
    *,
    session_id: str,
) -> tuple[
    tuple[tuple[SessionRecord, int], ...],
    tuple[SessionDiagnostic, ...],
    bool,
]:
    accepted: list[tuple[SessionRecord, int]] = []
    diagnostics: list[SessionDiagnostic] = []
    repair_required = False
    previous_sequence = 0
    line_number = 0
    try:
        if messages_path.is_symlink() or not messages_path.is_file():
            raise SessionError("session_not_found")
        with messages_path.open("rb") as stream:
            while True:
                payload = stream.readline(SESSION_RECORD_BYTES + 1)
                if not payload:
                    break
                line_number += 1
                if len(payload) > SESSION_RECORD_BYTES:
                    while payload and not payload.endswith(b"\n"):
                        payload = stream.readline(SESSION_RECORD_BYTES + 1)
                    diagnostics.append(
                        SessionDiagnostic(
                            line_number,
                            "session_line_too_large",
                        )
                    )
                    repair_required = True
                    continue
                if not payload.endswith(b"\n"):
                    diagnostics.append(
                        SessionDiagnostic(
                            line_number,
                            "session_line_missing_newline",
                        )
                    )
                    repair_required = True
                elif payload.endswith(b"\r\n"):
                    diagnostics.append(
                        SessionDiagnostic(
                            line_number,
                            "session_line_invalid_newline",
                        )
                    )
                    repair_required = True
                try:
                    decoded = payload.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    diagnostics.append(
                        SessionDiagnostic(
                            line_number,
                            "session_line_invalid_utf8",
                        )
                    )
                    repair_required = True
                    continue
                try:
                    raw = json.loads(
                        decoded,
                        object_pairs_hook=_pairs_to_dict,
                    )
                except (json.JSONDecodeError, _DuplicateKeyError):
                    diagnostics.append(
                        SessionDiagnostic(
                            line_number,
                            "session_line_invalid_json",
                        )
                    )
                    repair_required = True
                    continue
                try:
                    record = SessionRecord.from_dict(
                        raw,
                        expected_session_id=session_id,
                    )
                except (TypeError, ValueError):
                    diagnostics.append(
                        SessionDiagnostic(
                            line_number,
                            "session_line_invalid_schema",
                        )
                    )
                    repair_required = True
                    continue
                if record.sequence <= previous_sequence:
                    diagnostics.append(
                        SessionDiagnostic(
                            line_number,
                            "session_line_sequence_not_increasing",
                        )
                    )
                    repair_required = True
                    continue
                accepted.append((record, line_number))
                previous_sequence = record.sequence
    except SessionError:
        raise
    except OSError as exc:
        raise SessionError("session_resume_failed") from exc
    return tuple(accepted), tuple(diagnostics), repair_required


def _complete_tool_prefix(
    records: tuple[tuple[SessionRecord, int], ...],
) -> tuple[int, int | None]:
    index = 0
    while index < len(records):
        message = records[index][0].message
        if message.role == "tool":
            return index, records[index][1]
        if message.role != "assistant" or not message.tool_calls:
            index += 1
            continue
        expected_ids = tuple(call.call_id for call in message.tool_calls)
        if len(expected_ids) != len(set(expected_ids)):
            return index, records[index][1]
        end = index + 1 + len(expected_ids)
        if end > len(records):
            return index, records[index][1]
        actual_ids = tuple(
            record.message.tool_call_id
            if record.message.role == "tool"
            else None
            for record, _line_number in records[index + 1 : end]
        )
        if actual_ids != expected_ids:
            return index, records[index][1]
        index = end
    return len(records), None


def _rewrite_records(
    path: Path,
    records: tuple[SessionRecord, ...],
) -> tuple[SessionRecord, ...]:
    normalized = tuple(
        SessionRecord(
            record.session_id,
            index,
            record.created_at,
            record.message,
        )
        for index, record in enumerate(records, start=1)
    )
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY_FLAG,
            0o600,
        )
        for record in normalized:
            line = _json_bytes(record.to_dict(), newline=True)
            if len(line) > SESSION_RECORD_BYTES:
                raise OSError("normalized record exceeds line limit")
            _write_all(descriptor, line)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        _set_private_file(temporary)
        os.replace(temporary, path)
        _set_private_file(path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return normalized


def recover_session(
    *,
    sessions_root: Path,
    session_id: str,
    project_root: Path,
    provider_id: str,
    model: str,
    now_factory: Callable[[], datetime] = lambda: datetime.now().astimezone(),
) -> SessionRecovery:
    """Recover, truncate invalid tool tails, and repair one session."""

    directory = _session_directory(sessions_root, session_id)
    messages_path = directory / _MESSAGES_FILENAME
    scanned, initial_diagnostics, repair_required = _scan_records(
        messages_path,
        session_id=session_id,
    )
    diagnostics = list(initial_diagnostics)
    prefix_end, invalid_tool_line = _complete_tool_prefix(scanned)
    if prefix_end != len(scanned):
        assert invalid_tool_line is not None
        diagnostics.append(
            SessionDiagnostic(
                invalid_tool_line,
                "session_tool_batch_invalid",
            )
        )
        scanned = scanned[:prefix_end]
        repair_required = True

    records = tuple(record for record, _line_number in scanned)
    if any(
        record.sequence != index
        for index, record in enumerate(records, start=1)
    ):
        repair_required = True

    normalized_project_root = str(project_root.resolve(strict=True))
    meta_path = directory / _META_FILENAME
    try:
        existing_meta = load_session_meta(
            meta_path,
            expected_session_id=session_id,
        )
    except SessionError:
        existing_meta = None
    if (
        existing_meta is not None
        and existing_meta.project_root != normalized_project_root
    ):
        raise SessionError("session_access_denied")

    if repair_required:
        try:
            records = _rewrite_records(messages_path, records)
        except OSError as exc:
            raise SessionError("session_repair_failed") from exc

    meta_needs_rebuild = existing_meta is None or not _meta_matches_records(
        existing_meta,
        records,
    )
    if meta_needs_rebuild:
        fallback_timestamp = _timestamp(now_factory)
        meta = _build_meta(
            session_id=session_id,
            project_root=normalized_project_root,
            provider_id=(
                existing_meta.provider_id
                if existing_meta is not None
                else provider_id
            ),
            model=existing_meta.model if existing_meta is not None else model,
            records=records,
            fallback_timestamp=fallback_timestamp,
        )
        try:
            _atomic_write(
                meta_path,
                _json_bytes(meta.to_dict(), newline=True),
            )
        except OSError as exc:
            raise SessionError("session_repair_failed") from exc
    else:
        assert existing_meta is not None
        meta = existing_meta

    return SessionRecovery(
        messages=tuple(record.message for record in records),
        meta=meta,
        diagnostics=tuple(diagnostics),
        repaired=repair_required or meta_needs_rebuild,
    )


class SessionJournal:
    """Synchronous append recorder for one active lazy session."""

    def __init__(
        self,
        *,
        sessions_root: Path,
        session_id: str,
        project_root: Path,
        provider_id: str,
        model: str,
        recovered_meta: SessionMeta | None = None,
        recovered_messages: tuple[ChatMessage, ...] = (),
        now_factory: Callable[[], datetime] = (
            lambda: datetime.now().astimezone()
        ),
    ) -> None:
        validate_session_id(session_id)
        normalized_project_root = str(project_root.resolve(strict=True))
        if recovered_meta is not None:
            if (
                recovered_meta.session_id != session_id
                or recovered_meta.project_root != normalized_project_root
                or recovered_meta.message_count != len(recovered_messages)
            ):
                raise ValueError("recovered session 状态不一致")
        self._sessions_root = sessions_root.resolve(strict=False)
        self._directory = self._sessions_root / session_id
        self._session_id = session_id
        self._project_root = normalized_project_root
        self._provider_id = provider_id
        self._model = model
        self._meta = recovered_meta
        self._has_user = any(
            message.role == "user" for message in recovered_messages
        )
        self._now_factory = now_factory
        self._initialized = recovered_meta is not None
        self._poisoned = False
        self._closed = False
        self._lock = threading.Lock()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def meta(self) -> SessionMeta | None:
        return self._meta

    def _initialize_directory(self) -> None:
        self._sessions_root.mkdir(parents=True, exist_ok=True)
        _set_private_directory(self._sessions_root)
        self._directory.mkdir(mode=0o700, exist_ok=False)
        _set_private_directory(self._directory)
        self._initialized = True

    def append(self, message: ChatMessage) -> None:
        if not isinstance(message, ChatMessage):
            raise ValueError("message 类型无效")
        with self._lock:
            if self._closed or self._poisoned:
                raise SessionError("session_write_failed")
            try:
                timestamp = _timestamp(self._now_factory)
                sequence = (
                    self._meta.last_sequence + 1
                    if self._meta is not None
                    else 1
                )
                record = SessionRecord(
                    self._session_id,
                    sequence,
                    timestamp,
                    message,
                )
                line = _json_bytes(record.to_dict(), newline=True)
                if len(line) > SESSION_RECORD_BYTES:
                    raise SessionError("session_record_too_large")
                if not self._initialized:
                    self._initialize_directory()

                current_meta = self._meta
                created_at = (
                    current_meta.created_at
                    if current_meta is not None
                    else timestamp
                )
                title = (
                    current_meta.title
                    if current_meta is not None
                    else "新会话"
                )
                if message.role == "user" and not self._has_user:
                    title = _title((message,))
                summary = (
                    current_meta.summary
                    if current_meta is not None
                    else ""
                )
                if message.role == "assistant" and not message.tool_calls:
                    summary = _summary((message,))
                next_meta = SessionMeta(
                    session_id=self._session_id,
                    project_root=self._project_root,
                    provider_id=(
                        current_meta.provider_id
                        if current_meta is not None
                        else self._provider_id
                    ),
                    model=(
                        current_meta.model
                        if current_meta is not None
                        else self._model
                    ),
                    title=title,
                    summary=summary,
                    message_count=(
                        current_meta.message_count + 1
                        if current_meta is not None
                        else 1
                    ),
                    last_sequence=sequence,
                    created_at=created_at,
                    updated_at=timestamp,
                )
                _append_line(self._directory / _MESSAGES_FILENAME, line)
                _atomic_write(
                    self._directory / _META_FILENAME,
                    _json_bytes(next_meta.to_dict(), newline=True),
                )
            except SessionError as exc:
                if exc.code != "session_record_too_large":
                    self._poisoned = True
                raise
            except (OSError, ValueError) as exc:
                self._poisoned = True
                raise SessionError("session_write_failed") from exc
            self._meta = next_meta
            if message.role == "user":
                self._has_user = True

    def close(self) -> None:
        with self._lock:
            self._closed = True
