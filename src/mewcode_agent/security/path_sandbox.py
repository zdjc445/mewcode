"""Canonical path containment for built-in tools."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
import os
from pathlib import Path
import re


class PathSandboxError(RuntimeError):
    """Raised when a path escapes every configured sandbox root."""


class PathSandbox:
    def __init__(
        self,
        working_directory: Path,
        *,
        allowed_roots: tuple[Path, ...] | None = None,
    ) -> None:
        try:
            working = working_directory.resolve(strict=True)
            roots = allowed_roots or (working,)
            resolved_roots = tuple(root.resolve(strict=True) for root in roots)
        except (OSError, RuntimeError) as exc:
            raise PathSandboxError("无法初始化路径沙箱") from exc
        if not working.is_dir():
            raise PathSandboxError("工作目录不是目录")
        if not resolved_roots or any(not root.is_dir() for root in resolved_roots):
            raise PathSandboxError("路径沙箱根目录无效")
        if not self._contained_in_any(working, resolved_roots):
            raise PathSandboxError("工作目录不在路径沙箱根目录内")
        self._working_directory = working
        self._roots = resolved_roots
        self._working_directory_binding: ContextVar[Path | None] = ContextVar(
            f"mewcode_path_sandbox_{id(self)}",
            default=None,
        )

    @property
    def working_directory(self) -> Path:
        return self._working_directory_binding.get() or self._working_directory

    @property
    def roots(self) -> tuple[Path, ...]:
        bound = self._working_directory_binding.get()
        return self._roots if bound is None else (bound,)

    @contextmanager
    def bind_working_directory(self, path: Path) -> Iterator[Path]:
        if not isinstance(path, Path) or not path.is_absolute():
            raise PathSandboxError("绑定工作目录必须是绝对 Path")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PathSandboxError("无法解析绑定工作目录") from exc
        if not resolved.is_dir():
            raise PathSandboxError("绑定工作目录不是目录")
        token = self._working_directory_binding.set(resolved)
        try:
            yield resolved
        finally:
            self._working_directory_binding.reset(token)

    def resolve(self, value: str | Path) -> Path:
        if isinstance(value, str):
            candidate = Path(value).expanduser()
        elif isinstance(value, Path):
            candidate = value.expanduser()
        else:
            raise PathSandboxError("路径参数类型无效")
        if not candidate.is_absolute():
            candidate = self.working_directory / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PathSandboxError("无法解析工具路径") from exc
        if not self._contained_in_any(resolved, self.roots):
            raise PathSandboxError("工具路径超出允许目录")
        return resolved

    def relative_posix(self, value: str | Path) -> str:
        resolved = self.resolve(value)
        for root in self.roots:
            if self._contained(resolved, root):
                relative = resolved.relative_to(root)
                return "." if not relative.parts else relative.as_posix()
        raise PathSandboxError("工具路径超出允许目录")

    @staticmethod
    def pattern_is_safe(pattern: str) -> bool:
        if not isinstance(pattern, str) or not pattern:
            return False
        normalized = pattern.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            return False
        return ".." not in normalized.split("/")

    @classmethod
    def _contained_in_any(
        cls,
        candidate: Path,
        roots: tuple[Path, ...],
    ) -> bool:
        return any(cls._contained(candidate, root) for root in roots)

    @staticmethod
    def _contained(candidate: Path, root: Path) -> bool:
        candidate_text = os.path.normcase(str(candidate))
        root_text = os.path.normcase(str(root))
        try:
            return os.path.commonpath((candidate_text, root_text)) == root_text
        except ValueError:
            return False
