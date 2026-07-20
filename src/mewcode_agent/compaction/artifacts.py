"""Session-scoped storage for externalized tool results."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

from mewcode_agent.compaction.models import (
    ArtifactReference,
    CompactionConfig,
    ContextCompactionError,
)


_SESSION_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


class ContextArtifactStore:
    """Own and validate artifacts created by one application session."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        session_id: str | None = None,
        config: CompactionConfig | None = None,
    ) -> None:
        self._config = config or CompactionConfig()
        selected_session_id = session_id or uuid4().hex
        if _SESSION_ID_PATTERN.fullmatch(selected_session_id) is None:
            raise ValueError("artifact session_id 必须是 32 位小写十六进制")
        if root is None:
            try:
                root = Path.home() / ".mewcode-agent" / "context-artifacts"
            except (OSError, RuntimeError) as exc:
                raise ContextCompactionError(
                    "context_artifact_write_failed",
                    "无法解析上下文 artifact 目录",
                ) from exc
        self._root = root.expanduser().resolve(strict=False)
        self._session_id = selected_session_id
        self._session_directory = self._root / selected_session_id
        self._tool_results_directory = self._session_directory / "tool-results"
        self._references: dict[str, ArtifactReference] = {}
        self._stored_bytes = 0
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def root(self) -> Path:
        return self._root

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_directory(self) -> Path:
        return self._session_directory

    async def initialize(self) -> None:
        async with self._lock:
            if self._closed:
                raise ContextCompactionError(
                    "context_artifact_write_failed",
                    "上下文 artifact store 已关闭",
                )
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        try:
            self._tool_results_directory.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )
            if os.name != "nt":
                self._root.chmod(0o700)
                self._session_directory.chmod(0o700)
                self._tool_results_directory.chmod(0o700)
        except OSError as exc:
            raise ContextCompactionError(
                "context_artifact_write_failed",
                "无法创建上下文 artifact 目录",
            ) from exc

    async def write(self, content: str) -> ArtifactReference:
        if not isinstance(content, str):
            raise TypeError("artifact content 必须是字符串")
        payload = content.encode("utf-8")
        if len(payload) > self._config.artifact_bytes:
            raise ContextCompactionError(
                "context_artifact_too_large",
                "工具结果超过单个 artifact 上限",
            )
        digest = sha256(payload).hexdigest()
        async with self._lock:
            if self._closed:
                raise ContextCompactionError(
                    "context_artifact_write_failed",
                    "上下文 artifact store 已关闭",
                )
            existing = self._references.get(digest)
            if existing is not None:
                return existing
            if self._stored_bytes + len(payload) > self._config.artifact_session_bytes:
                raise ContextCompactionError(
                    "context_artifact_budget_exceeded",
                    "当前会话的上下文 artifact 空间已用尽",
                )
            await asyncio.to_thread(self._initialize_sync)
            reference = await asyncio.to_thread(
                self._write_sync,
                payload,
                digest,
            )
            self._references[digest] = reference
            self._stored_bytes += len(payload)
            return reference

    def _write_sync(self, payload: bytes, digest: str) -> ArtifactReference:
        path = self._tool_results_directory / f"{digest}.json"
        temporary = self._tool_results_directory / (
            f".{digest}.{uuid4().hex}.tmp"
        )
        try:
            if path.exists():
                existing = path.read_bytes()
                if sha256(existing).hexdigest() != digest:
                    raise ContextCompactionError(
                        "context_artifact_corrupted",
                        "上下文 artifact 摘要校验失败",
                    )
            else:
                with temporary.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if os.name != "nt":
                    temporary.chmod(0o600)
                os.replace(temporary, path)
                if os.name != "nt":
                    path.chmod(0o600)
        except ContextCompactionError:
            raise
        except OSError as exc:
            raise ContextCompactionError(
                "context_artifact_write_failed",
                "无法写入上下文 artifact",
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink()
        return ArtifactReference(path.resolve(strict=True), digest, len(payload))

    async def read(
        self,
        path_text: str,
        *,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        if not isinstance(path_text, str) or not path_text:
            raise ContextCompactionError(
                "context_artifact_access_denied",
                "上下文 artifact 路径未获授权",
            )
        if type(offset) is not int or offset < 0:
            raise ValueError("offset 必须是非负整数")
        if (
            type(limit) is not int
            or not 1 <= limit <= self._config.artifact_read_characters
        ):
            raise ValueError(
                "limit 必须位于 1 与 artifact 读取上限之间"
            )
        async with self._lock:
            if self._closed:
                raise ContextCompactionError(
                    "context_artifact_not_found",
                    "上下文 artifact 已不可用",
                )
            reference = next(
                (
                    item
                    for item in self._references.values()
                    if str(item.path) == path_text
                ),
                None,
            )
            if reference is None:
                raise ContextCompactionError(
                    "context_artifact_access_denied",
                    "上下文 artifact 路径未获授权",
                )
            content = await asyncio.to_thread(
                self._read_validated_sync,
                reference,
            )
        selected = content[offset : offset + limit]
        next_offset = offset + len(selected)
        return {
            "path": str(reference.path),
            "sha256": reference.sha256,
            "content": selected,
            "offset": offset,
            "limit": limit,
            "total_characters": len(content),
            "has_more": next_offset < len(content),
            "next_offset": next_offset,
        }

    def _read_validated_sync(self, reference: ArtifactReference) -> str:
        try:
            path = reference.path
            if path.is_symlink() or not path.is_file():
                raise ContextCompactionError(
                    "context_artifact_not_found",
                    "上下文 artifact 不存在",
                )
            resolved = path.resolve(strict=True)
            if resolved != reference.path:
                raise ContextCompactionError(
                    "context_artifact_access_denied",
                    "上下文 artifact 路径未获授权",
                )
            payload = resolved.read_bytes()
        except ContextCompactionError:
            raise
        except FileNotFoundError as exc:
            raise ContextCompactionError(
                "context_artifact_not_found",
                "上下文 artifact 不存在",
            ) from exc
        except OSError as exc:
            raise ContextCompactionError(
                "context_artifact_not_found",
                "无法读取上下文 artifact",
            ) from exc
        if len(payload) != reference.utf8_bytes or sha256(payload).hexdigest() != (
            reference.sha256
        ):
            raise ContextCompactionError(
                "context_artifact_corrupted",
                "上下文 artifact 摘要校验失败",
            )
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContextCompactionError(
                "context_artifact_corrupted",
                "上下文 artifact 不是有效 UTF-8",
            ) from exc

    async def cleanup_stale(self, *, now: float | None = None) -> None:
        selected_now = time.time() if now is None else now
        if type(selected_now) not in (int, float):
            raise TypeError("now 必须是数字")
        await asyncio.to_thread(self._cleanup_stale_sync, float(selected_now))

    def _cleanup_stale_sync(self, now: float) -> None:
        if not self._root.is_dir():
            return
        try:
            candidates = tuple(self._root.iterdir())
        except OSError:
            return
        for candidate in candidates:
            if (
                candidate.name == self._session_id
                or _SESSION_ID_PATTERN.fullmatch(candidate.name) is None
                or candidate.is_symlink()
                or not candidate.is_dir()
            ):
                continue
            try:
                age = now - candidate.stat().st_mtime
                resolved = candidate.resolve(strict=True)
                if (
                    age <= self._config.stale_artifact_seconds
                    or resolved.parent != self._root
                ):
                    continue
                shutil.rmtree(resolved)
            except OSError:
                continue

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._references.clear()
            self._stored_bytes = 0
            await asyncio.to_thread(self._remove_current_session_sync)

    def _remove_current_session_sync(self) -> None:
        path = self._session_directory
        try:
            if not path.exists() or path.is_symlink():
                return
            resolved = path.resolve(strict=True)
            if (
                resolved.parent != self._root
                or resolved.name != self._session_id
            ):
                return
            shutil.rmtree(resolved)
        except OSError:
            return
