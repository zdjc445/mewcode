from __future__ import annotations

from pathlib import Path

from mewcode_agent import cli


def test_cli_returns_one_when_config_is_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert cli.main() == 1
    assert "配置文件不存在" in capsys.readouterr().err


def test_cli_returns_one_when_api_key_is_missing(
    tmp_path: Path,
    valid_config_text: str,
    monkeypatch,
    capsys,
) -> None:
    (tmp_path / "llm_providers.yaml").write_text(
        valid_config_text,
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert cli.main() == 1
    error = capsys.readouterr().err
    assert "DEEPSEEK_API_KEY 缺失或为空" in error


def test_cli_builds_and_runs_app_with_valid_config(
    tmp_path: Path,
    valid_config_text: str,
    monkeypatch,
) -> None:
    (tmp_path / "llm_providers.yaml").write_text(
        valid_config_text,
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    run_calls: list[bool] = []
    monkeypatch.setattr(cli.ChatApp, "run", lambda self: run_calls.append(True))

    assert cli.main() == 0
    assert run_calls == [True]
