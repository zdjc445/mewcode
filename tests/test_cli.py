from __future__ import annotations

from pathlib import Path

from mewcode_agent import cli
from mewcode_agent.prompting.environment import PromptEnvironmentError
from mewcode_agent.tools.registry import ToolRegistry


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
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    run_calls: list[bool] = []
    agent_loop_calls: list[dict[str, object]] = []

    class FakeAgentLoop:
        def __init__(
            self,
            provider: object,
            registry: ToolRegistry,
            *,
            prompt_runtime: object,
            prompt_composer: object,
            scheduler: object,
        ) -> None:
            agent_loop_calls.append(
                {
                    "provider": provider,
                    "registry": registry,
                    "prompt_runtime": prompt_runtime,
                    "prompt_composer": prompt_composer,
                    "scheduler": scheduler,
                }
            )

    monkeypatch.setattr(cli, "AgentLoop", FakeAgentLoop, raising=False)
    monkeypatch.setattr(cli.ChatApp, "run", lambda self: run_calls.append(True))

    assert cli.main() == 0
    assert run_calls == [True]
    assert len(agent_loop_calls) == 1
    registry = agent_loop_calls[0]["registry"]
    assert isinstance(registry, ToolRegistry)
    assert registry.get("read_file") is not None
    assert agent_loop_calls[0]["scheduler"] is not None


def test_cli_reports_invalid_security_config(
    tmp_path: Path,
    valid_config_text: str,
    monkeypatch,
    capsys,
) -> None:
    (tmp_path / "llm_providers.yaml").write_text(
        valid_config_text,
        encoding="utf-8",
    )
    home_path = tmp_path / "home"
    security_path = home_path / ".mewcode-agent" / "security.yaml"
    security_path.parent.mkdir(parents=True)
    security_path.write_text(
        "version: 1\nmode: unsafe\nrules: []\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: home_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")

    assert cli.main() == 1
    error = capsys.readouterr().err
    assert "启动失败：" in error
    assert "mode 必须为 strict、default 或 permissive" in error


def test_cli_loads_security_layers_and_injects_policy_scheduler(
    tmp_path: Path,
    valid_config_text: str,
    monkeypatch,
) -> None:
    (tmp_path / "llm_providers.yaml").write_text(
        valid_config_text,
        encoding="utf-8",
    )
    home_path = tmp_path / "home"
    user_security = home_path / ".mewcode-agent" / "security.yaml"
    project_security = tmp_path / ".mewcode" / "security.yaml"
    user_security.parent.mkdir(parents=True)
    project_security.parent.mkdir(parents=True)
    user_security.write_text(
        "version: 1\nmode: strict\nrules: []\n",
        encoding="utf-8",
    )
    project_security.write_text(
        """version: 1
rules:
  - id: project.allow_tests
    action: allow
    tool: run_command
    priority: 10
    match:
      command:
        kind: glob
        pattern: "uv run pytest*"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: home_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    calls: list[dict[str, object]] = []

    class FakeAgentLoop:
        def __init__(
            self,
            provider: object,
            registry: ToolRegistry,
            **kwargs: object,
        ) -> None:
            calls.append(
                {"provider": provider, "registry": registry, **kwargs}
            )

    monkeypatch.setattr(cli, "AgentLoop", FakeAgentLoop)
    monkeypatch.setattr(cli.ChatApp, "run", lambda self: None)

    assert cli.main() == 0
    scheduler = calls[0]["scheduler"]
    policy = scheduler._policy_engine  # type: ignore[union-attr]
    assert policy.mode == "strict"


def test_cli_builds_prompt_dependencies_from_exact_two_layers(
    tmp_path: Path,
    valid_config_text: str,
    monkeypatch,
) -> None:
    (tmp_path / "llm_providers.yaml").write_text(
        valid_config_text,
        encoding="utf-8",
    )
    home_path = tmp_path / "home"
    user_path = home_path / ".mewcode-agent" / "prompts.yaml"
    project_path = tmp_path / ".mewcode" / "prompts.yaml"
    user_path.parent.mkdir(parents=True)
    project_path.parent.mkdir(parents=True)
    user_path.write_text(
        "version: 1\nmodules:\n"
        "  - id: coding.team\n    enabled: true\n"
        "    priority: 510\n    content: user team\n"
        "  - id: output.user_extra\n    enabled: true\n"
        "    priority: 810\n    content: user extra\n",
        encoding="utf-8",
    )
    project_path.write_text(
        "version: 1\nmodules:\n"
        "  - id: coding.team\n    enabled: true\n"
        "    priority: 505\n    content: project team\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: home_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    calls: list[dict[str, object]] = []

    class FakeAgentLoop:
        def __init__(
            self,
            provider: object,
            registry: ToolRegistry,
            **kwargs: object,
        ) -> None:
            calls.append(
                {"provider": provider, "registry": registry, **kwargs}
            )

    monkeypatch.setattr(cli, "AgentLoop", FakeAgentLoop)
    monkeypatch.setattr(cli.ChatApp, "run", lambda self: None)

    assert cli.main() == 0
    assert len(calls) == 1
    assert set(calls[0]) == {
        "provider",
        "registry",
        "prompt_runtime",
        "prompt_composer",
        "scheduler",
    }
    composer = calls[0]["prompt_composer"]
    frame = composer.compose([], ())  # type: ignore[union-attr]
    assert "## coding.team\nproject team" in frame.system_prompt
    assert "## output.user_extra\nuser extra" in frame.system_prompt
    assert "user team" not in frame.system_prompt


def test_cli_reports_prompt_config_error_without_content(
    tmp_path: Path,
    valid_config_text: str,
    monkeypatch,
    capsys,
) -> None:
    (tmp_path / "llm_providers.yaml").write_text(
        valid_config_text,
        encoding="utf-8",
    )
    prompt_path = tmp_path / ".mewcode" / "prompts.yaml"
    prompt_path.parent.mkdir()
    prompt_path.write_text(
        "version: 1\nmodules:\n  - id: core.safety\n"
        "    enabled: true\n    priority: 1\n"
        "    content: SECRET_BODY\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")

    assert cli.main() == 1
    error = capsys.readouterr().err
    assert "启动失败：" in error
    assert str(prompt_path) in error
    assert "core" in error
    assert "SECRET_BODY" not in error


def test_cli_reports_prompt_environment_error(
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
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(
        cli,
        "collect_session_environment",
        lambda: (_ for _ in ()).throw(
            PromptEnvironmentError("cwd error")
        ),
    )

    assert cli.main() == 1
    assert "启动失败：cwd error" in capsys.readouterr().err


def test_cli_sanitizes_path_cwd_failure(monkeypatch, capsys) -> None:
    def fail_cwd() -> Path:
        raise OSError("SECRET_CWD")

    monkeypatch.setattr(Path, "cwd", fail_cwd)

    assert cli.main() == 1
    error = capsys.readouterr().err
    assert "启动失败：无法解析当前工作目录" in error
    assert "SECRET_CWD" not in error


def test_cli_sanitizes_path_home_failure(monkeypatch, capsys) -> None:
    def fail_home() -> Path:
        raise RuntimeError("SECRET_HOME")

    monkeypatch.setattr(Path, "home", fail_home)

    assert cli.main() == 1
    error = capsys.readouterr().err
    assert "启动失败：无法解析用户全局 Prompt 配置路径" in error
    assert "SECRET_HOME" not in error
