from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.worktrees import (
    WorktreeConfigError,
    WorktreeRuntimeConfig,
    load_worktree_config,
)


VALID_CONFIG = """version: 1
stale_after_hours: 72
cleanup_interval_seconds: 1800
local_config_files:
  - settings.local.json
dependency_links:
  - .venv
copy_ignored:
  - build/cache
"""


def test_missing_config_uses_defaults_without_creating_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worktrees.yaml"

    assert load_worktree_config(path) == WorktreeRuntimeConfig()
    assert not path.exists()


def test_loads_exact_worktree_config(tmp_path: Path) -> None:
    path = tmp_path / "worktrees.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    config = load_worktree_config(path)

    assert config.dependency_links == (".venv",)
    assert config.copy_ignored == ("build/cache",)


@pytest.mark.parametrize(
    "content",
    [
        "version: 1\n",
        VALID_CONFIG + "unknown: true\n",
        VALID_CONFIG.replace("version: 1", "version: 2"),
        VALID_CONFIG.replace("stale_after_hours: 72", "stale_after_hours: true"),
        VALID_CONFIG.replace(
            "version: 1\n", "version: 1\nversion: 1\n"
        ),
        VALID_CONFIG.replace(
            "  - settings.local.json", "  - ../settings.local.json"
        ),
        VALID_CONFIG.replace(
            "  - build/cache", "  - build/cache\n  - build/cache"
        ),
    ],
)
def test_rejects_invalid_or_nonexact_config(
    tmp_path: Path,
    content: str,
) -> None:
    path = tmp_path / "worktrees.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(WorktreeConfigError) as caught:
        load_worktree_config(path)

    assert caught.value.code == "worktree_config_invalid"
