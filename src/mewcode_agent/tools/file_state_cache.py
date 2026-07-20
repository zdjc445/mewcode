"""Thread-safe snapshots used to guard file writes and edits."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from mewcode_agent.tools.base import ToolExecutionError


@dataclass(frozen=True, slots=True)
class FileState:
    """Filesystem metadata captured after a successful read or write."""

    mtime_ns: int
    size: int


class FileStateCache:
    """Track the last observed state of files by their resolved paths."""

    def __init__(self) -> None:
        self._states: dict[Path, FileState] = {}
        self._bound_states: ContextVar[dict[Path, FileState] | None] = (
            ContextVar(
                f"mewcode_file_state_{id(self)}",
                default=None,
            )
        )
        self._lock = Lock()

    def _current_states(self) -> dict[Path, FileState]:
        bound = self._bound_states.get()
        return self._states if bound is None else bound

    @contextmanager
    def isolated(self) -> Iterator[None]:
        """Bind a fresh state mapping to this async execution context."""

        token = self._bound_states.set({})
        try:
            yield
        finally:
            self._bound_states.reset(token)

    def record(self, path: Path) -> None:
        resolved_path = path.resolve()
        stat = resolved_path.stat()
        state = FileState(mtime_ns=stat.st_mtime_ns, size=stat.st_size)
        with self._lock:
            self._current_states()[resolved_path] = state

    def ensure_current(self, path: Path) -> None:
        resolved_path = path.resolve()
        stat = resolved_path.stat()
        current_state = FileState(mtime_ns=stat.st_mtime_ns, size=stat.st_size)
        with self._lock:
            expected_state = self._current_states().get(resolved_path)
        if expected_state is None:
            raise ToolExecutionError(
                "file_not_read",
                "文件尚未读取，请先使用 read_file",
            )

        if current_state != expected_state:
            raise ToolExecutionError(
                "file_changed_since_read",
                "文件在读取后已被修改，请重新读取",
            )
