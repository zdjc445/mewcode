from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import subprocess

import pytest

from mewcode_agent.worktrees import (
    GitRunner,
    WorktreeError,
    read_linked_worktree_head,
    worktree_branch_name,
)


HEAD = "a" * 40


class _HangingProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.waited = False
        self.stdout = _HangingStream()
        self.stderr = _HangingStream()

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        self.returncode = -15
        return self.returncode


class _HangingStream:
    async def read(self, _size: int) -> bytes:
        await asyncio.Event().wait()
        return b""


class _CompletedProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.terminated = False
        self.waited = False

    async def wait(self) -> int:
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _linked_layout(tmp_path: Path, *, loose: bool) -> tuple[Path, Path, str]:
    root = (tmp_path / "worktree").resolve()
    common = (tmp_path / "git-common").resolve()
    gitdir = common / "worktrees" / "managed"
    root.mkdir()
    gitdir.mkdir(parents=True)
    branch = worktree_branch_name("feature/cache")
    (root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (gitdir / "HEAD").write_text(
        f"ref: refs/heads/{branch}\n",
        encoding="utf-8",
    )
    if loose:
        ref = common / "refs" / "heads" / branch
        ref.parent.mkdir(parents=True)
        ref.write_text(f"{HEAD}\n", encoding="utf-8")
    else:
        (common / "packed-refs").write_text(
            f"# pack-refs with: peeled fully-peeled sorted\n{HEAD} refs/heads/{branch}\n",
            encoding="utf-8",
        )
    return root, common, branch


@pytest.mark.parametrize("loose", [True, False])
def test_reads_linked_worktree_head_without_git(
    tmp_path: Path,
    loose: bool,
) -> None:
    root, common, branch = _linked_layout(tmp_path, loose=loose)

    assert read_linked_worktree_head(
        root,
        common_git_dir=common,
        expected_branch=branch,
    ) == HEAD


def test_recovery_rejects_detached_or_wrong_branch(tmp_path: Path) -> None:
    root, common, branch = _linked_layout(tmp_path, loose=True)
    gitdir = common / "worktrees" / "managed"
    (gitdir / "HEAD").write_text(f"{HEAD}\n", encoding="utf-8")

    with pytest.raises(WorktreeError) as caught:
        read_linked_worktree_head(
            root,
            common_git_dir=common,
            expected_branch=branch,
        )

    assert caught.value.code == "worktree_recovery_failed"


async def test_git_runner_uses_repository_identity(tmp_path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git executable is unavailable")
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    subprocess.run(
        [git, "-C", str(root), "init"],
        check=True,
        capture_output=True,
    )
    runner = GitRunner()

    identity = await runner.repository_identity(root)

    assert identity.main_root == root
    assert identity.common_git_dir == (root / ".git").resolve()


async def test_git_runner_maps_nonzero_without_stderr(tmp_path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git executable is unavailable")
    runner = GitRunner()

    with pytest.raises(WorktreeError) as caught:
        await runner.run(
            tmp_path.resolve(),
            "rev-parse",
            "--verify",
            "refs/heads/definitely-missing",
            error_code="worktree_repository_unavailable",
        )

    assert caught.value.code == "worktree_repository_unavailable"
    assert "fatal" not in caught.value.message


async def test_git_runner_timeout_terminates_and_reaps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = tmp_path / "git.exe"
    executable.touch()
    process = _HangingProcess()
    captured: tuple[tuple[object, ...], dict[str, object]] | None = None

    async def create_process(*args, **kwargs):
        nonlocal captured
        captured = (args, kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    runner = GitRunner(git_finder=lambda _name: str(executable))

    with pytest.raises(WorktreeError) as caught:
        await runner.run(
            tmp_path.resolve(),
            "status",
            timeout_seconds=0.01,
        )

    assert caught.value.code == "worktree_git_timeout"
    assert process.terminated is True
    assert process.waited is True
    assert process.killed is False
    assert captured is not None
    assert captured[0][:4] == (
        str(executable.resolve()),
        "-C",
        str(tmp_path.resolve()),
        "status",
    )
    assert "shell" not in captured[1]


async def test_git_runner_cancellation_terminates_and_reaps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = tmp_path / "git.exe"
    executable.touch()
    process = _HangingProcess()

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    runner = GitRunner(git_finder=lambda _name: str(executable))
    task = asyncio.create_task(runner.run(tmp_path.resolve(), "status"))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated is True
    assert process.waited is True


async def test_git_runner_enforces_output_limit_before_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = tmp_path / "git.exe"
    executable.touch()
    process = _CompletedProcess(b"12345")

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    runner = GitRunner(
        git_finder=lambda _name: str(executable),
        output_limit=4,
    )

    with pytest.raises(WorktreeError) as caught:
        await runner.run(tmp_path.resolve(), "status")

    assert caught.value.code == "worktree_status_failed"
    assert process.terminated is True
    assert process.waited is True
