"""Public session persistence API."""

from mewcode_agent.sessions.models import (
    SESSION_ID_PATTERN,
    SESSION_RECORD_BYTES,
    SESSION_SCHEMA_VERSION,
    SessionDiagnostic,
    SessionDiagnosticCode,
    SessionError,
    SessionErrorCode,
    SessionCommand,
    SessionCommandKind,
    SessionDeleteTarget,
    SessionMeta,
    SessionRecord,
    SessionRecovery,
    chat_message_from_dict,
    chat_message_to_dict,
    validate_session_id,
)
from mewcode_agent.sessions.manager import (
    SessionManager,
    parse_session_command,
)
from mewcode_agent.sessions.storage import (
    SessionJournal,
    load_session_meta,
    recover_session,
)

__all__ = [
    "SESSION_ID_PATTERN",
    "SESSION_RECORD_BYTES",
    "SESSION_SCHEMA_VERSION",
    "SessionDiagnostic",
    "SessionDiagnosticCode",
    "SessionError",
    "SessionErrorCode",
    "SessionCommand",
    "SessionCommandKind",
    "SessionDeleteTarget",
    "SessionJournal",
    "SessionManager",
    "SessionMeta",
    "SessionRecord",
    "SessionRecovery",
    "chat_message_from_dict",
    "chat_message_to_dict",
    "load_session_meta",
    "parse_session_command",
    "recover_session",
    "validate_session_id",
]
