from __future__ import annotations

import os
from pathlib import Path

import pytest

from mewcode_agent.worktrees import (
    GitCommandResult,
    WorktreeInitializer,
    WorktreeRuntimeConfig,
)


class _FakeGit:
    def __init__(
        self,
        *,
        hooks_path: str | None = None,
        ignored: dict[str, int] | None = None,
    ) -> None:
        self.hooks_path = hooks_path
        self.ignored = ignored or {}
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    async def run(self, cwd: Path, *arguments: str, **_kwargs):
        self.calls.append((cwd, arguments))
        if arguments == ("config", "--get", "core.hooksPath"):
            if self.hooks_path is None:
                return GitCommandResult(1, "")
            return GitCommandResult(0, self.hooks_path)
        if arguments[:4] == ("check-ignore", "-q", "--", arguments[-1]):
            return GitCommandResult(self.ignored.get(arguments[-1], 1), "")
        return GitCommandResult(0, "")


async def test_initializes_copy_link_and_ignored_paths(tmp_path: Path) -> None:
    main = (tmp_path / "main").resolve()
    target = (tmp_path / "target").resolve()
    main.mkdir()
    target.mkdir()
    (main / "settings.local.json").write_text("local", encoding="utf-8")
    (main / "config").mkdir()
    (main / "config" / "nested.txt").write_text("nested", encoding="utf-8")
    (main / "deps").mkdir()
    (main / "cache").mkdir()
    (main / "cache" / "item.txt").write_text("ignored", encoding="utf-8")
    git = _FakeGit(ignored={"cache": 0})
    initializer = WorktreeInitializer(
        main_root=main,
        config=WorktreeRuntimeConfig(
            local_config_files=("settings.local.json", "config"),
            dependency_links=("deps",),
            copy_ignored=("cache",),
        ),
        git=git,  # type: ignore[arg-type]
    )

    diagnostics = await initializer.initialize(target)

    dependency_diagnostics = [
        item for item in diagnostics if item.stage == "dependency_link"
    ]
    assert (target / "settings.local.json").read_text(encoding="utf-8") == "local"
    assert (target / "config" / "nested.txt").read_text(encoding="utf-8") == "nested"
    if (target / "deps").is_symlink():
        assert dependency_diagnostics == []
        assert (target / "deps").resolve() == (main / "deps").resolve()
    else:
        assert [item.code for item in dependency_diagnostics] == [
            "worktree_dependency_link_failed"
        ]
        assert not (target / "deps").exists()
    assert (target / "cache" / "item.txt").read_text(encoding="utf-8") == "ignored"


async def test_existing_destination_is_not_overwritten(tmp_path: Path) -> None:
    main = (tmp_path / "main").resolve()
    target = (tmp_path / "target").resolve()
    main.mkdir()
    target.mkdir()
    (main / "settings.local.json").write_text("source", encoding="utf-8")
    (target / "settings.local.json").write_text("target", encoding="utf-8")
    initializer = WorktreeInitializer(
        main_root=main,
        config=WorktreeRuntimeConfig(),
        git=_FakeGit(),  # type: ignore[arg-type]
    )

    diagnostics = await initializer.initialize(target)

    assert diagnostics == ()
    assert (target / "settings.local.json").read_text(encoding="utf-8") == "target"


async def test_configures_absolute_worktree_hooks_path(tmp_path: Path) -> None:
    main = (tmp_path / "main").resolve()
    target = (tmp_path / "target").resolve()
    main.mkdir()
    target.mkdir()
    git = _FakeGit(hooks_path=".githooks")
    initializer = WorktreeInitializer(
        main_root=main,
        config=WorktreeRuntimeConfig(local_config_files=()),
        git=git,  # type: ignore[arg-type]
    )

    diagnostics = await initializer.initialize(target)

    assert diagnostics == ()
    assert git.calls == [
        (main, ("config", "--get", "core.hooksPath")),
        (main, ("config", "extensions.worktreeConfig", "true")),
        (
            target,
            (
                "config",
                "--worktree",
                "core.hooksPath",
                str((main / ".githooks").resolve()),
            ),
        ),
    ]


async def test_records_not_ignored_and_dependency_failures(tmp_path: Path) -> None:
    main = (tmp_path / "main").resolve()
    target = (tmp_path / "target").resolve()
    main.mkdir()
    target.mkdir()
    (main / "tracked.txt").write_text("tracked", encoding="utf-8")
    initializer = WorktreeInitializer(
        main_root=main,
        config=WorktreeRuntimeConfig(
            local_config_files=(),
            dependency_links=("missing",),
            copy_ignored=("tracked.txt",),
        ),
        git=_FakeGit(),  # type: ignore[arg-type]
    )

    diagnostics = await initializer.initialize(target)

    assert [(item.stage, item.path, item.code) for item in diagnostics] == [
        ("dependency_link", "missing", "worktree_dependency_link_failed"),
        ("copy_ignored", "tracked.txt", "worktree_ignored_not_ignored"),
    ]


async def test_source_symlink_escape_is_refused(tmp_path: Path) -> None:
    main = (tmp_path / "main").resolve()
    target = (tmp_path / "target").resolve()
    outside = (tmp_path / "outside.txt").resolve()
    main.mkdir()
    target.mkdir()
    outside.write_text("secret", encoding="utf-8")
    source = main / "settings.local.json"
    try:
        source.symlink_to(outside)
    except OSError:
        pytest.skip("File symlink creation is unavailable")
    initializer = WorktreeInitializer(
        main_root=main,
        config=WorktreeRuntimeConfig(),
        git=_FakeGit(),  # type: ignore[arg-type]
    )

    diagnostics = await initializer.initialize(target)

    assert [item.code for item in diagnostics] == [
        "worktree_local_config_failed"
    ]
    assert not os.path.lexists(target / "settings.local.json")
