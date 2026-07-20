from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.teams import TeamConfigError, TeamRuntimeConfig, load_team_config


VALID = """\
version: 1
max_teams: 8
max_members_per_team: 8
max_tasks_per_team: 256
scheduler_interval_seconds: 1
member_timeout_seconds: 900
member_history_messages: 40
"""


def test_missing_config_uses_defaults(tmp_path: Path) -> None:
    assert load_team_config(tmp_path / "missing.yaml") == TeamRuntimeConfig()


def test_loads_exact_config(tmp_path: Path) -> None:
    path = tmp_path / "teams.yaml"
    path.write_text(VALID, encoding="utf-8")

    assert load_team_config(path) == TeamRuntimeConfig()


@pytest.mark.parametrize(
    "text",
    [
        VALID + "max_teams: 9\n",
        VALID + "unknown: true\n",
        VALID.replace("max_teams: 8\n", ""),
        VALID.replace("version: 1", "version: true"),
        VALID.replace("max_teams: 8", "max_teams: true"),
        VALID.replace("member_timeout_seconds: 900", "member_timeout_seconds: 29"),
        VALID.replace("member_history_messages: 40", "member_history_messages: 3"),
    ],
)
def test_rejects_nonexact_or_invalid_config(tmp_path: Path, text: str) -> None:
    path = tmp_path / "teams.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(TeamConfigError) as caught:
        load_team_config(path)

    assert caught.value.code == "team_config_invalid"
