from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode_agent.prompting.environment import (
    GitEnvironment,
    GitRequestEnvironmentCollector,
    PromptEnvironmentError,
    RequestEnvironment,
    RequestEnvironmentCollector,
    SessionEnvironment,
    collect_session_environment,
)


def test_collect_windows_session_environment_uses_command_contract(
    tmp_path: Path,
) -> None:
    now = datetime(
        2026,
        7,
        18,
        12,
        0,
        tzinfo=timezone(timedelta(hours=8), "China Standard Time"),
    )

    result = collect_session_environment(
        working_directory=tmp_path,
        platform_name="Windows",
        now=now,
    )

    assert result.operating_system == "Windows"
    assert result.shell == "powershell.exe"
    assert result.working_directory == str(tmp_path.resolve())
    assert result.timezone_name == "China Standard Time"
    assert result.utc_offset == "+08:00"
    assert result.to_json() == (
        '{"operating_system":"Windows","shell":"powershell.exe",'
        f'"working_directory":"{str(tmp_path.resolve()).replace(chr(92), chr(92) * 2)}",'
        '"timezone":{"name":"China Standard Time","utc_offset":"+08:00"}}'
    )


def test_request_environment_json_has_fixed_shape() -> None:
    result = RequestEnvironment(
        current_time="2026-07-18T12:00:00+08:00",
        git=GitEnvironment("repository", "master", " M file.py", None),
    )

    assert result.to_json() == (
        '{"current_time":"2026-07-18T12:00:00+08:00",'
        '"git":{"state":"repository","branch":"master",'
        '"worktree_status":" M file.py","reason":null}}'
    )


class RecordingRunner:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.calls: list[tuple[tuple[str, ...], float]] = []

    async def run(self, argv: tuple[str, ...], timeout: float) -> object:
        self.calls.append((argv, timeout))
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_non_repository_does_not_run_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_exists = Path.exists

    def hide_repository_markers(path: Path) -> bool:
        return False if path.name == ".git" else real_exists(path)

    monkeypatch.setattr(Path, "exists", hide_repository_markers)
    runner = RecordingRunner([])
    collector = GitRequestEnvironmentCollector(
        working_directory=tmp_path,
        runner=runner,
        git_path_finder=lambda _: "C:/Git/bin/git.exe",
        now_factory=lambda: datetime.now(timezone.utc),
    )

    result = await collector.collect()

    assert result.git == GitEnvironment("not_repository", None, None, None)
    assert runner.calls == []


@pytest.mark.asyncio
async def test_repository_runs_exact_commands_and_only_strips_trailing_newlines(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    runner = RecordingRunner(
        [
            SimpleNamespace(returncode=0, stdout=b"feature/x\r\n", stderr=b""),
            SimpleNamespace(
                returncode=0,
                stdout=b" M one.py\r\n?? two.py\n",
                stderr=b"",
            ),
        ]
    )
    collector = GitRequestEnvironmentCollector(
        working_directory=tmp_path,
        runner=runner,
        git_path_finder=lambda _: "C:/Git/bin/git.exe",
        now_factory=lambda: datetime(2026, 7, 18, tzinfo=timezone.utc),
    )

    result = await collector.collect()

    cwd = str(tmp_path.resolve())
    assert runner.calls == [
        (("C:/Git/bin/git.exe", "-C", cwd, "branch", "--show-current"), 10.0),
        (("C:/Git/bin/git.exe", "-C", cwd, "status", "--short"), 10.0),
    ]
    assert result.git == GitEnvironment(
        "repository",
        "feature/x",
        " M one.py\r\n?? two.py",
        None,
    )


@pytest.mark.asyncio
async def test_parent_repository_marker_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    working_directory = repository / "nested"
    (repository / ".git").mkdir(parents=True)
    working_directory.mkdir()
    expected_marker = repository / ".git"
    real_exists = Path.exists

    def expose_only_expected_marker(path: Path) -> bool:
        if path.name == ".git":
            return path == expected_marker
        return real_exists(path)

    monkeypatch.setattr(Path, "exists", expose_only_expected_marker)
    runner = RecordingRunner(
        [
            SimpleNamespace(returncode=0, stdout=b"main\n", stderr=b""),
            SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
        ]
    )
    collector = GitRequestEnvironmentCollector(
        working_directory=working_directory,
        runner=runner,
        git_path_finder=lambda _: "git",
        now_factory=lambda: datetime.now(timezone.utc),
    )

    result = await collector.collect()

    assert result.git == GitEnvironment("repository", "main", "", None)
    assert len(runner.calls) == 2


@pytest.mark.asyncio
async def test_missing_git_and_failed_command_are_unavailable(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").write_text(
        "gitdir: ../repo/.git",
        encoding="utf-8",
    )
    missing = GitRequestEnvironmentCollector(
        working_directory=tmp_path,
        runner=RecordingRunner([]),
        git_path_finder=lambda _: None,
        now_factory=lambda: datetime.now(timezone.utc),
    )
    failed_runner = RecordingRunner(
        [SimpleNamespace(returncode=128, stdout=b"", stderr=b"secret")]
    )
    failed = GitRequestEnvironmentCollector(
        working_directory=tmp_path,
        runner=failed_runner,
        git_path_finder=lambda _: "git",
        now_factory=lambda: datetime.now(timezone.utc),
    )

    missing_result = await missing.collect()
    failed_result = await failed.collect()

    assert missing_result.git.reason == "git_executable_not_found"
    assert failed_result.git.reason == "branch_exit_128"
    assert "secret" not in failed_result.git.reason


@pytest.mark.parametrize(
    "arguments",
    [
        ("repository", None, "", None),
        ("not_repository", "master", None, None),
        ("unavailable", None, None, None),
    ],
)
def test_git_environment_rejects_state_field_mismatch(
    arguments: tuple[object, object, object, object],
) -> None:
    with pytest.raises(ValueError, match="state"):
        GitEnvironment(*arguments)  # type: ignore[arg-type]


def test_non_windows_session_uses_bin_sh(tmp_path: Path) -> None:
    result = collect_session_environment(
        working_directory=tmp_path,
        platform_name="Linux",
        now=datetime.now(timezone.utc),
    )

    assert result.shell == "/bin/sh"


def test_working_directory_resolution_failure_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_resolve(self: Path, strict: bool = False) -> Path:
        raise OSError("raw cwd detail")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    with pytest.raises(
        PromptEnvironmentError,
        match="无法解析当前工作目录",
    ):
        collect_session_environment()


class ExceptionRunner:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def run(self, argv: tuple[str, ...], timeout: float) -> object:
        raise self._error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [(TimeoutError(), "branch_timeout"), (OSError(), "branch_OSError")],
)
async def test_git_runner_exception_is_sanitized(
    tmp_path: Path,
    error: Exception,
    reason: str,
) -> None:
    (tmp_path / ".git").mkdir()
    collector = GitRequestEnvironmentCollector(
        working_directory=tmp_path,
        runner=ExceptionRunner(error),
        git_path_finder=lambda _: "git",
        now_factory=lambda: datetime.now(timezone.utc),
    )

    result = await collector.collect()

    assert result.git.reason == reason


@pytest.mark.asyncio
async def test_request_time_requires_utc_offset(tmp_path: Path) -> None:
    collector = GitRequestEnvironmentCollector(
        working_directory=tmp_path,
        runner=RecordingRunner([]),
        git_path_finder=lambda _: "git",
        now_factory=lambda: datetime(2026, 7, 18, 12, 0),
    )

    with pytest.raises(PromptEnvironmentError, match="UTC offset"):
        await collector.collect()


def test_environment_api_is_exported_from_prompting_package() -> None:
    from mewcode_agent import prompting

    assert prompting.GitEnvironment is GitEnvironment
    assert (
        prompting.GitRequestEnvironmentCollector
        is GitRequestEnvironmentCollector
    )
    assert prompting.PromptEnvironmentError is PromptEnvironmentError
    assert prompting.RequestEnvironment is RequestEnvironment
    assert (
        prompting.RequestEnvironmentCollector is RequestEnvironmentCollector
    )
    assert prompting.SessionEnvironment is SessionEnvironment
    assert prompting.collect_session_environment is collect_session_environment
