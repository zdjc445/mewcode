"""Shell-free Git execution and pure linked-worktree HEAD recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import shutil

from mewcode_agent.worktrees.models import WorktreeError, validate_object_id


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stdout: str


@dataclass(frozen=True, slots=True)
class GitRepositoryIdentity:
    main_root: Path
    common_git_dir: Path


class GitRunner:
    def __init__(
        self,
        *,
        git_finder: Callable[[str], str | None] = shutil.which,
        output_limit: int = 1024 * 1024,
    ) -> None:
        git = git_finder("git")
        if git is None:
            raise WorktreeError(
                "worktree_git_unavailable",
                "Git executable 不可用",
            )
        self._git = str(Path(git).resolve(strict=True))
        self._output_limit = output_limit

    async def run(
        self,
        cwd: Path,
        *arguments: str,
        timeout_seconds: float = 30.0,
        check: bool = True,
        error_code: str = "worktree_status_failed",
    ) -> GitCommandResult:
        if not isinstance(cwd, Path) or not cwd.is_absolute():
            raise ValueError("Git cwd 必须是绝对 Path")
        try:
            process = await asyncio.create_subprocess_exec(
                self._git,
                "-C",
                str(cwd),
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise WorktreeError(error_code, "Git 子进程启动失败") from exc
        try:
            async with asyncio.timeout(timeout_seconds):
                stdout, stderr = await process.communicate()
        except TimeoutError as exc:
            await self._stop(process)
            raise WorktreeError("worktree_git_timeout", "Git 命令超时") from exc
        except asyncio.CancelledError:
            await self._stop(process)
            raise
        if len(stdout) > self._output_limit or len(stderr) > self._output_limit:
            raise WorktreeError(error_code, "Git 命令输出超过限制")
        try:
            decoded = stdout.decode("utf-8", errors="strict").rstrip("\r\n")
            stderr.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise WorktreeError(error_code, "Git 命令输出不是 UTF-8") from exc
        result = GitCommandResult(process.returncode or 0, decoded)
        if check and result.returncode != 0:
            raise WorktreeError(error_code, "Git 命令返回非零状态")
        return result

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            async with asyncio.timeout(2):
                await process.wait()
            return
        except TimeoutError:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()

    async def repository_identity(self, cwd: Path) -> GitRepositoryIdentity:
        try:
            root_result = await self.run(
                cwd,
                "rev-parse",
                "--show-toplevel",
                error_code="worktree_repository_unavailable",
            )
            root = Path(root_result.stdout).resolve(strict=True)
            common_result = await self.run(
                cwd,
                "rev-parse",
                "--git-common-dir",
                error_code="worktree_repository_unavailable",
            )
            raw_common = Path(common_result.stdout)
            common = (
                raw_common
                if raw_common.is_absolute()
                else root / raw_common
            ).resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise WorktreeError(
                "worktree_repository_unavailable",
                "无法解析 Git 仓库",
            ) from exc
        if not root.is_dir() or not common.is_dir():
            raise WorktreeError(
                "worktree_repository_unavailable",
                "Git 仓库目录无效",
            )
        return GitRepositoryIdentity(root, common)


def read_linked_worktree_head(
    worktree_root: Path,
    *,
    common_git_dir: Path,
    expected_branch: str,
) -> str:
    root = worktree_root.resolve(strict=True)
    common = common_git_dir.resolve(strict=True)
    marker = root / ".git"
    if not marker.is_file():
        raise WorktreeError(
            "worktree_recovery_failed",
            "linked worktree .git 文件无效",
        )
    try:
        marker_text = marker.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise WorktreeError(
            "worktree_recovery_failed",
            "无法读取 linked worktree .git 文件",
        ) from exc
    prefix = "gitdir: "
    if not marker_text.startswith(prefix) or "\n" in marker_text:
        raise WorktreeError(
            "worktree_recovery_failed",
            "linked worktree .git 内容无效",
        )
    raw_gitdir = Path(marker_text[len(prefix) :])
    try:
        gitdir = (
            raw_gitdir if raw_gitdir.is_absolute() else root / raw_gitdir
        ).resolve(strict=True)
        gitdir.relative_to((common / "worktrees").resolve(strict=True))
        head_text = (gitdir / "HEAD").read_text(encoding="utf-8").rstrip(
            "\r\n"
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise WorktreeError(
            "worktree_recovery_failed",
            "无法解析 linked worktree HEAD",
        ) from exc
    expected_ref = f"refs/heads/{expected_branch}"
    if head_text != f"ref: {expected_ref}":
        raise WorktreeError(
            "worktree_recovery_failed",
            "linked worktree branch 不匹配",
        )
    loose = common / _ref_path(expected_ref)
    if loose.is_file():
        try:
            return validate_object_id(
                loose.read_text(encoding="utf-8").rstrip("\r\n")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise WorktreeError(
                "worktree_recovery_failed",
                "无法读取 branch ref",
            ) from exc
    packed = common / "packed-refs"
    try:
        lines = packed.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise WorktreeError(
            "worktree_recovery_failed",
            "无法读取 packed-refs",
        ) from exc
    matches = [
        line.split(" ", 1)[0]
        for line in lines
        if not line.startswith(("#", "^"))
        and " " in line
        and line.split(" ", 1)[1] == expected_ref
    ]
    if len(matches) != 1:
        raise WorktreeError(
            "worktree_recovery_failed",
            "packed branch ref 不唯一或不存在",
        )
    try:
        return validate_object_id(matches[0])
    except ValueError as exc:
        raise WorktreeError(
            "worktree_recovery_failed",
            "packed branch object ID 无效",
        ) from exc


def _ref_path(value: str) -> Path:
    parts = value.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise WorktreeError("worktree_recovery_failed", "Git ref 无效")
    return Path(*parts)
