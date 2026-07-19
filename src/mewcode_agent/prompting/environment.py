"""Environment collection for runtime prompt context."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
import platform
from pathlib import Path
import shutil
from typing import Literal, Protocol, TypeAlias

GitState: TypeAlias = Literal[
    "repository",
    "not_repository",
    "unavailable",
]


class PromptEnvironmentError(RuntimeError):
    """A safe startup error for required environment state."""


@dataclass(frozen=True, slots=True)
class SessionEnvironment:
    operating_system: str
    shell: str
    working_directory: str
    timezone_name: str | None
    utc_offset: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "operating_system": self.operating_system,
                "shell": self.shell,
                "working_directory": self.working_directory,
                "timezone": {
                    "name": self.timezone_name,
                    "utc_offset": self.utc_offset,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class GitEnvironment:
    state: GitState
    branch: str | None
    worktree_status: str | None
    reason: str | None

    def __post_init__(self) -> None:
        repository = (
            self.state == "repository"
            and isinstance(self.branch, str)
            and isinstance(self.worktree_status, str)
            and self.reason is None
        )
        empty = (
            self.state == "not_repository"
            and self.branch is None
            and self.worktree_status is None
            and self.reason is None
        )
        unavailable = (
            self.state == "unavailable"
            and self.branch is None
            and self.worktree_status is None
            and isinstance(self.reason, str)
            and bool(self.reason)
        )
        if not (repository or empty or unavailable):
            raise ValueError("GitEnvironment 字段与 state 不一致")


@dataclass(frozen=True, slots=True)
class RequestEnvironment:
    current_time: str
    git: GitEnvironment

    def to_json(self) -> str:
        return json.dumps(
            {
                "current_time": self.current_time,
                "git": {
                    "state": self.git.state,
                    "branch": self.git.branch,
                    "worktree_status": self.git.worktree_status,
                    "reason": self.git.reason,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def _utc_offset(now: datetime) -> str:
    offset = now.utcoffset()
    if offset is None:
        raise PromptEnvironmentError("无法取得本地 UTC 偏移")
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def collect_session_environment(
    *,
    working_directory: Path | None = None,
    platform_name: str | None = None,
    now: datetime | None = None,
) -> SessionEnvironment:
    try:
        cwd = (working_directory or Path.cwd()).resolve(strict=True)
    except OSError as exc:
        raise PromptEnvironmentError("无法解析当前工作目录") from exc
    actual_platform = platform_name or platform.system()
    current = now or datetime.now().astimezone()
    return SessionEnvironment(
        operating_system=actual_platform,
        shell="powershell.exe" if actual_platform == "Windows" else "/bin/sh",
        working_directory=str(cwd),
        timezone_name=current.tzname(),
        utc_offset=_utc_offset(current),
    )


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class AsyncCommandRunner(Protocol):
    async def run(
        self,
        argv: tuple[str, ...],
        timeout: float,
    ) -> CommandResult: ...


class RequestEnvironmentCollector(Protocol):
    async def collect(self) -> RequestEnvironment: ...


class SubprocessCommandRunner:
    async def run(
        self,
        argv: tuple[str, ...],
        timeout: float,
    ) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        return CommandResult(process.returncode or 0, stdout, stderr)


class GitRequestEnvironmentCollector:
    def __init__(
        self,
        *,
        working_directory: Path,
        runner: AsyncCommandRunner | None = None,
        git_path_finder: Callable[[str], str | None] = shutil.which,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._working_directory = working_directory.resolve(strict=True)
        self._runner = runner or SubprocessCommandRunner()
        self._git_path_finder = git_path_finder
        self._now_factory = now_factory or (
            lambda: datetime.now().astimezone()
        )

    def _has_repository_marker(self) -> bool:
        return any(
            (candidate / ".git").exists()
            for candidate in (
                self._working_directory,
                *self._working_directory.parents,
            )
        )

    async def _command(
        self,
        git_path: str,
        stage: str,
        *arguments: str,
    ) -> tuple[str | None, str | None]:
        argv = (
            git_path,
            "-C",
            str(self._working_directory),
            *arguments,
        )
        try:
            result = await self._runner.run(argv, 10.0)
        except TimeoutError:
            return None, f"{stage}_timeout"
        except OSError as exc:
            return None, f"{stage}_{type(exc).__name__}"
        if result.returncode != 0:
            return None, f"{stage}_exit_{result.returncode}"
        return (
            result.stdout.decode("utf-8", errors="replace").rstrip("\r\n"),
            None,
        )

    async def collect(self) -> RequestEnvironment:
        current = self._now_factory()
        if current.utcoffset() is None:
            raise PromptEnvironmentError("当前时间必须包含 UTC offset")
        if not self._has_repository_marker():
            git = GitEnvironment("not_repository", None, None, None)
        else:
            git_path = self._git_path_finder("git")
            if git_path is None:
                git = GitEnvironment(
                    "unavailable",
                    None,
                    None,
                    "git_executable_not_found",
                )
            else:
                branch, reason = await self._command(
                    git_path,
                    "branch",
                    "branch",
                    "--show-current",
                )
                if reason is None:
                    status, reason = await self._command(
                        git_path,
                        "status",
                        "status",
                        "--short",
                    )
                else:
                    status = None
                git = (
                    GitEnvironment("repository", branch, status, None)
                    if reason is None
                    else GitEnvironment("unavailable", None, None, reason)
                )
        return RequestEnvironment(current.isoformat(), git)
