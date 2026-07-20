"""Public automatic-notes API."""

from mewcode_agent.notes.models import (
    NoteClearTarget,
    NoteErrorCode,
    NotePaths,
    NoteScope,
    NoteWarning,
    NotesError,
    NotesSnapshot,
)
from mewcode_agent.notes.manager import (
    NOTES_EXIT_TIMEOUT_SECONDS,
    NOTES_TRIGGER_REQUESTS,
    NotesManager,
)
from mewcode_agent.notes.storage import (
    NOTES_FILE_BYTES,
    load_notes,
    note_paths,
    render_project_notes,
    render_user_notes,
    write_note_scope,
)
from mewcode_agent.notes.updater import (
    NOTES_INPUT_BYTES,
    NOTES_RECENT_UNITS,
    NOTES_RESPONSE_BYTES,
    NOTES_SYSTEM_PROMPT,
    NoteGeneration,
    NoteUpdater,
)

__all__ = [
    "NOTES_FILE_BYTES",
    "NOTES_INPUT_BYTES",
    "NOTES_EXIT_TIMEOUT_SECONDS",
    "NOTES_RECENT_UNITS",
    "NOTES_RESPONSE_BYTES",
    "NOTES_SYSTEM_PROMPT",
    "NOTES_TRIGGER_REQUESTS",
    "NoteClearTarget",
    "NoteErrorCode",
    "NotePaths",
    "NoteGeneration",
    "NoteUpdater",
    "NoteScope",
    "NoteWarning",
    "NotesError",
    "NotesSnapshot",
    "NotesManager",
    "load_notes",
    "note_paths",
    "render_project_notes",
    "render_user_notes",
    "write_note_scope",
]
