from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.worktrees import (
    WorktreeRuntimeConfig,
    managed_worktree_path,
    validate_relative_config_path,
    validate_worktree_name,
    worktree_branch_name,
)


@pytest.mark.parametrize(
    "name",
    [
        "a",
        "feature/cache-report",
        "a" * 32,
        "/".join(("a" * 24, "b" * 23, "c" * 23, "d" * 23)),
    ],
)
def test_accepts_strict_worktree_names(name: str) -> None:
    assert validate_worktree_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "A",
        "1a",
        "/a",
        "a/",
        "a//b",
        "a/./b",
        "a/../b",
        r"a\b",
        "a/b/c/d/e",
        "a" * 33,
        "con",
        "lpt9",
        "a" * 97,
    ],
)
def test_rejects_noncanonical_worktree_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_worktree_name(name)


def test_branch_mapping_is_stable_bounded_and_collision_resistant() -> None:
    nested = worktree_branch_name("a/b")
    flat = worktree_branch_name("a-b")
    longest = worktree_branch_name(
        "/".join(("a" * 24, "b" * 23, "c" * 23, "d" * 23))
    )

    assert nested.startswith("mewcode-wt-a-b-")
    assert nested != flat
    assert nested == worktree_branch_name("a/b")
    assert len(longest) <= 120


def test_managed_path_uses_name_segments(tmp_path: Path) -> None:
    root = (tmp_path / "managed").resolve()

    result = managed_worktree_path(root, "feature/cache")

    assert result == root / "feature" / "cache"


@pytest.mark.parametrize(
    "value",
    [
        "settings.local.json",
        ".env",
        "nested/config.toml",
    ],
)
def test_accepts_posix_relative_config_paths(value: str) -> None:
    assert validate_relative_config_path(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute",
        "C:/absolute",
        "trailing/",
        "a//b",
        "./a",
        "a/../b",
        r"a\b",
        ".git",
        ".git/config",
        ".mewcode/.worktrees",
        ".mewcode/.worktrees/cache",
        "nul\x00byte",
    ],
)
def test_rejects_unsafe_config_paths(value: str) -> None:
    with pytest.raises(ValueError):
        validate_relative_config_path(value)


def test_runtime_config_validates_integer_ranges_and_duplicates() -> None:
    assert WorktreeRuntimeConfig() == WorktreeRuntimeConfig(
        stale_after_hours=72,
        cleanup_interval_seconds=1800,
        local_config_files=("settings.local.json",),
    )
    with pytest.raises(ValueError):
        WorktreeRuntimeConfig(stale_after_hours=True)
    with pytest.raises(ValueError):
        WorktreeRuntimeConfig(cleanup_interval_seconds=59)
    with pytest.raises(ValueError):
        WorktreeRuntimeConfig(copy_ignored=("cache", "cache"))
