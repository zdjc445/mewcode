from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.config import ConfigError, load_config


def test_load_valid_config(config_path: Path) -> None:
    config = load_config(
        config_path,
        environ={"DEEPSEEK_API_KEY": "test-secret"},
    )

    assert config.default_provider == "deepseek_openai"
    assert config.active_provider.base_url == "https://api.deepseek.com"
    assert config.active_provider.model == "deepseek-v4-pro"
    assert config.active_provider.max_tokens == 4096
    assert config.active_provider.context_window_tokens == 1000000
    assert config.api_key == "test-secret"
    assert "test-secret" not in repr(config)


def test_load_anthropic_as_default(
    config_path: Path,
    valid_config_text: str,
) -> None:
    config_path.write_text(
        valid_config_text.replace(
            "default_provider: deepseek_openai",
            "default_provider: deepseek_anthropic",
        ),
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        environ={"DEEPSEEK_API_KEY": "test-secret"},
    )

    assert config.active_provider.protocol == "anthropic"
    assert config.active_provider.base_url == "https://api.deepseek.com/anthropic"


def test_missing_config_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(
            tmp_path / "llm_providers.yaml",
            environ={"DEEPSEEK_API_KEY": "test-secret"},
        )


def test_invalid_yaml_is_rejected(config_path: Path) -> None:
    config_path.write_text("providers: [", encoding="utf-8")

    with pytest.raises(ConfigError, match="不是有效 YAML"):
        load_config(config_path, environ={"DEEPSEEK_API_KEY": "test-secret"})


def test_missing_top_level_field_is_rejected(
    config_path: Path,
    valid_config_text: str,
) -> None:
    config_path.write_text(
        valid_config_text.replace("default_provider: deepseek_openai\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="缺少字段: default_provider"):
        load_config(config_path, environ={"DEEPSEEK_API_KEY": "test-secret"})


def test_unknown_provider_field_is_rejected(
    config_path: Path,
    valid_config_text: str,
) -> None:
    config_path.write_text(
        valid_config_text.replace(
            "    max_tokens: 4096\n",
            "    max_tokens: 4096\n    temperature: 0\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="包含未知字段: temperature"):
        load_config(config_path, environ={"DEEPSEEK_API_KEY": "test-secret"})


def test_wrong_protocol_is_rejected(
    config_path: Path,
    valid_config_text: str,
) -> None:
    config_path.write_text(
        valid_config_text.replace("protocol: openai", "protocol: OpenAI", 1),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="protocol 必须为 openai"):
        load_config(config_path, environ={"DEEPSEEK_API_KEY": "test-secret"})


def test_wrong_max_tokens_type_is_rejected(
    config_path: Path,
    valid_config_text: str,
) -> None:
    config_path.write_text(
        valid_config_text.replace("max_tokens: 4096", 'max_tokens: "4096"', 1),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="max_tokens 必须是整数"):
        load_config(config_path, environ={"DEEPSEEK_API_KEY": "test-secret"})


@pytest.mark.parametrize("value", ['"1000000"', "true"])
def test_wrong_context_window_tokens_type_is_rejected(
    config_path: Path,
    valid_config_text: str,
    value: str,
) -> None:
    config_path.write_text(
        valid_config_text.replace(
            "context_window_tokens: 1000000",
            f"context_window_tokens: {value}",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError,
        match="context_window_tokens 必须是整数",
    ):
        load_config(config_path, environ={"DEEPSEEK_API_KEY": "test-secret"})


def test_missing_provider_is_rejected(
    config_path: Path,
    valid_config_text: str,
) -> None:
    anthropic_block = """
  deepseek_anthropic:
    protocol: anthropic
    base_url: https://api.deepseek.com/anthropic
    api_key_env: DEEPSEEK_API_KEY
    model: deepseek-v4-pro
    max_tokens: 4096
    context_window_tokens: 1000000
"""
    config_path.write_text(
        valid_config_text.replace(anthropic_block, ""),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="缺少 deepseek_anthropic"):
        load_config(config_path, environ={"DEEPSEEK_API_KEY": "test-secret"})


def test_unknown_default_provider_is_rejected(
    config_path: Path,
    valid_config_text: str,
) -> None:
    config_path.write_text(
        valid_config_text.replace(
            "default_provider: deepseek_openai",
            "default_provider: missing_provider",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="default_provider 不存在"):
        load_config(config_path, environ={"DEEPSEEK_API_KEY": "test-secret"})


@pytest.mark.parametrize("environment", [{}, {"DEEPSEEK_API_KEY": "   "}])
def test_missing_or_blank_api_key_is_rejected(
    config_path: Path,
    environment: dict[str, str],
) -> None:
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY 缺失或为空"):
        load_config(config_path, environ=environment)
