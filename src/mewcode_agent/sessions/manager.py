"""Active-session lifecycle, listing, switching, and explicit delete."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import shutil
from uuid import uuid4

from mewcode_agent.history import ConversationHistory
from mewcode_agent.models import ChatMessage
from mewcode_agent.prompting.models import RuntimeInstruction
from mewcode_agent.sessions.models import (
    SessionDeleteTarget,
    SessionError,
    SessionMeta,
    SessionRecovery,
    validate_session_id,
)
from mewcode_agent.sessions.storage import (
    SessionJournal,
    load_session_meta,
    recover_session,
)

_RESUME_GAP_SECONDS = 7 * 24 * 60 * 60


class SessionManager:
    """Own the current journal without automatically deleting sessions."""

    def __init__(
        self,
        *,
        sessions_root: Path,
        project_root: Path,
        provider_id: str,
        model: str,
        history: ConversationHistory,
        id_factory: Callable[[], str] = lambda: uuid4().hex,
        now_factory: Callable[[], datetime] = (
            lambda: datetime.now().astimezone()
        ),
    ) -> None:
        try:
            self._sessions_root = sessions_root.resolve(strict=False)
            self._project_root = project_root.resolve(strict=True)
        except OSError as exc:
            raise SessionError("session_access_denied") from exc
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("provider_id 必须是非空字符串")
        if not isinstance(model, str) or not model:
            raise ValueError("model 必须是非空字符串")
        if not isinstance(history, ConversationHistory):
            raise ValueError("history 类型无效")
        session_id = id_factory()
        try:
            validate_session_id(session_id)
        except ValueError as exc:
            raise ValueError("id_factory 返回了无效 session_id") from exc
        self._provider_id = provider_id
        self._model = model
        self._history = history
        self._id_factory = id_factory
        self._now_factory = now_factory
        self._journal = self._new_journal(session_id)
        self._history.set_append_recorder(self._journal.append)

    def _new_journal(
        self,
        session_id: str,
        *,
        meta: SessionMeta | None = None,
        messages: tuple[ChatMessage, ...] = (),
    ) -> SessionJournal:
        return SessionJournal(
            sessions_root=self._sessions_root,
            session_id=session_id,
            project_root=self._project_root,
            provider_id=self._provider_id,
            model=self._model,
            recovered_meta=meta,
            recovered_messages=messages,
            now_factory=self._now_factory,
        )

    @property
    def active_session_id(self) -> str:
        return self._journal.session_id

    @property
    def active_directory(self) -> Path:
        return self._journal.directory

    def _validated_target(
        self,
        session_id: str,
    ) -> tuple[Path, SessionMeta]:
        try:
            validate_session_id(session_id)
        except ValueError as exc:
            raise SessionError("session_not_found") from exc
        directory = self._sessions_root / session_id
        try:
            if not directory.exists():
                raise SessionError("session_not_found")
            if directory.is_symlink() or not directory.is_dir():
                raise SessionError("session_access_denied")
            resolved = directory.resolve(strict=True)
            if resolved.parent != self._sessions_root:
                raise SessionError("session_access_denied")
            meta_path = resolved / "meta.json"
            if meta_path.is_symlink():
                raise SessionError("session_access_denied")
            meta = load_session_meta(
                meta_path,
                expected_session_id=session_id,
            )
        except SessionError:
            raise
        except OSError as exc:
            raise SessionError("session_access_denied") from exc
        if meta.project_root != str(self._project_root):
            raise SessionError("session_access_denied")
        return resolved, meta

    def list_sessions(self) -> tuple[SessionMeta, ...]:
        if not self._sessions_root.exists():
            return ()
        try:
            candidates = tuple(self._sessions_root.iterdir())
        except OSError as exc:
            raise SessionError("session_access_denied") from exc
        metas: list[SessionMeta] = []
        for candidate in candidates:
            try:
                validate_session_id(candidate.name)
                if candidate.is_symlink() or not candidate.is_dir():
                    continue
                resolved = candidate.resolve(strict=True)
                if resolved.parent != self._sessions_root:
                    continue
                meta = load_session_meta(
                    resolved / "meta.json",
                    expected_session_id=candidate.name,
                )
            except (OSError, ValueError, SessionError):
                continue
            if meta.project_root == str(self._project_root):
                metas.append(meta)
        metas.sort(key=lambda item: item.session_id)
        metas.sort(
            key=lambda item: datetime.fromisoformat(item.updated_at),
            reverse=True,
        )
        return tuple(metas)

    def session_path(self, session_id: str) -> Path:
        directory, _meta = self._validated_target(session_id)
        return directory

    def _reopen(
        self,
        *,
        session_id: str,
        meta: SessionMeta | None,
        messages: tuple[ChatMessage, ...],
    ) -> None:
        self._history.restore(messages)
        self._journal = self._new_journal(
            session_id,
            meta=meta,
            messages=messages,
        )
        self._history.set_append_recorder(self._journal.append)

    def resume(
        self,
        session_id: str,
        *,
        activate: Callable[[SessionRecovery], None] | None = None,
    ) -> SessionRecovery:
        previous_id = self._journal.session_id
        previous_meta = self._journal.meta
        previous_messages = tuple(self._history.snapshot())
        self._journal.close()
        try:
            recovery = recover_session(
                sessions_root=self._sessions_root,
                session_id=session_id,
                project_root=self._project_root,
                provider_id=self._provider_id,
                model=self._model,
                now_factory=self._now_factory,
            )
        except Exception:
            self._reopen(
                session_id=previous_id,
                meta=previous_meta,
                messages=previous_messages,
            )
            raise

        self._history.restore(recovery.messages)
        self._journal = self._new_journal(
            session_id,
            meta=recovery.meta,
            messages=recovery.messages,
        )
        self._history.set_append_recorder(self._journal.append)
        if activate is not None:
            try:
                activate(recovery)
            except Exception as exc:
                self._journal.close()
                self._reopen(
                    session_id=previous_id,
                    meta=previous_meta,
                    messages=previous_messages,
                )
                raise SessionError("session_resume_failed") from exc
        return recovery

    async def resume_async(
        self,
        session_id: str,
        *,
        activate: Callable[[SessionRecovery], None] | None = None,
    ) -> SessionRecovery:
        previous_id = self._journal.session_id
        previous_meta = self._journal.meta
        previous_messages = tuple(self._history.snapshot())
        self._journal.close()
        try:
            recovery = await asyncio.to_thread(
                recover_session,
                sessions_root=self._sessions_root,
                session_id=session_id,
                project_root=self._project_root,
                provider_id=self._provider_id,
                model=self._model,
                now_factory=self._now_factory,
            )
        except Exception:
            self._reopen(
                session_id=previous_id,
                meta=previous_meta,
                messages=previous_messages,
            )
            raise

        self._history.restore(recovery.messages)
        self._journal = self._new_journal(
            session_id,
            meta=recovery.meta,
            messages=recovery.messages,
        )
        self._history.set_append_recorder(self._journal.append)
        if activate is not None:
            try:
                activate(recovery)
            except Exception as exc:
                self._journal.close()
                self._reopen(
                    session_id=previous_id,
                    meta=previous_meta,
                    messages=previous_messages,
                )
                raise SessionError("session_resume_failed") from exc
        return recovery

    def start_new(
        self,
        *,
        activate: Callable[[], None] | None = None,
    ) -> str:
        previous_id = self._journal.session_id
        previous_meta = self._journal.meta
        previous_messages = tuple(self._history.snapshot())
        session_id = self._id_factory()
        try:
            validate_session_id(session_id)
        except ValueError as exc:
            raise SessionError("session_switch_failed") from exc
        if session_id == previous_id or (
            self._sessions_root / session_id
        ).exists():
            raise SessionError("session_switch_failed")

        self._journal.close()
        try:
            self._history.restore(())
            self._journal = self._new_journal(session_id)
            self._history.set_append_recorder(self._journal.append)
            if activate is not None:
                activate()
        except Exception as exc:
            self._journal.close()
            self._reopen(
                session_id=previous_id,
                meta=previous_meta,
                messages=previous_messages,
            )
            raise SessionError("session_switch_failed") from exc
        return session_id

    def resume_gap_instruction(
        self,
        meta: SessionMeta,
    ) -> RuntimeInstruction | None:
        current = self._now_factory()
        if current.utcoffset() is None:
            raise SessionError("session_resume_failed")
        previous = datetime.fromisoformat(meta.updated_at)
        elapsed_seconds = (current - previous).total_seconds()
        if elapsed_seconds < _RESUME_GAP_SECONDS:
            return None
        full_days = int(elapsed_seconds // (24 * 60 * 60))
        return RuntimeInstruction(
            "runtime.session.resume_gap",
            "context",
            "session",
            (
                f"会话恢复时间跨度：上次活跃时间={meta.updated_at}；"
                f"当前时间={current.isoformat()}；完整天数={full_days}。"
                "项目文件、依赖、分支和外部状态可能已经变化；"
                "得出结论前必须重新读取相关事实。"
            ),
            "session",
        )

    def prepare_delete(self, session_id: str) -> SessionDeleteTarget:
        if session_id == self.active_session_id:
            raise SessionError("session_delete_active")
        directory, meta = self._validated_target(session_id)
        return SessionDeleteTarget(session_id, meta.title, directory)

    def delete(self, target: SessionDeleteTarget) -> None:
        if not isinstance(target, SessionDeleteTarget):
            raise ValueError("target 类型无效")
        if target.session_id == self.active_session_id:
            raise SessionError("session_delete_active")
        directory, meta = self._validated_target(target.session_id)
        if directory != target.path or meta.session_id != target.session_id:
            raise SessionError("session_access_denied")
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise SessionError("session_delete_failed") from exc

    def close(self) -> None:
        self._history.set_append_recorder(None)
        self._journal.close()
