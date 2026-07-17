"""Thread-safe snapshots used to guard file writes and edits."""

from __future__ import annotations

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
        self._lock = Lock()

    def record(self, path: Path) -> None:
        resolved_path = path.resolve()
        stat = resolved_path.stat()
        state = FileState(mtime_ns=stat.st_mtime_ns, size=stat.st_size)
        with self._lock:
            self._states[resolved_path] = state

    def ensure_current(self, path: Path) -> None:
        resolved_path = path.resolve()
        stat = resolved_path.stat()
        current_state = FileState(mtime_ns=stat.st_mtime_ns, size=stat.st_size)
        with self._lock:
            expected_state = self._states.get(resolved_path)
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
