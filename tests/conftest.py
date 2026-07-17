from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.config import ProviderConfig


@pytest.fixture
def openai_config() -> ProviderConfig:
    return ProviderConfig(
        provider_id="deepseek_openai",
        protocol="openai",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        model="deepseek-v4-pro",
        max_tokens=4096,
    )


@pytest.fixture
def anthropic_config() -> ProviderConfig:
    return ProviderConfig(
        provider_id="deepseek_anthropic",
        protocol="anthropic",
        base_url="https://api.deepseek.com/anthropic",
        api_key_env="DEEPSEEK_API_KEY",
        model="deepseek-v4-pro",
        max_tokens=4096,
    )


@pytest.fixture
def valid_config_text() -> str:
    return """\
default_provider: deepseek_openai

providers:
  deepseek_openai:
    protocol: openai
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    model: deepseek-v4-pro
    max_tokens: 4096

  deepseek_anthropic:
    protocol: anthropic
    base_url: https://api.deepseek.com/anthropic
    api_key_env: DEEPSEEK_API_KEY
    model: deepseek-v4-pro
    max_tokens: 4096
"""


@pytest.fixture
def config_path(tmp_path: Path, valid_config_text: str) -> Path:
    path = tmp_path / "llm_providers.yaml"
    path.write_text(valid_config_text, encoding="utf-8")
    return path
