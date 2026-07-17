"""Strict loading for ``llm_providers.yaml``."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

import yaml

ProtocolName: TypeAlias = Literal["openai", "anthropic"]

_TOP_LEVEL_KEYS = {"default_provider", "providers"}
_PROVIDER_KEYS = {"protocol", "base_url", "api_key_env", "model", "max_tokens"}
_EXPECTED_PROVIDERS: dict[str, dict[str, str | int]] = {
    "deepseek_openai": {
        "protocol": "openai",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-pro",
        "max_tokens": 4096,
    },
    "deepseek_anthropic": {
        "protocol": "anthropic",
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-pro",
        "max_tokens": 4096,
    },
}


class ConfigError(RuntimeError):
    """A safe, user-facing configuration error."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider_id: str
    protocol: ProtocolName
    base_url: str
    api_key_env: str
    model: str
    max_tokens: int


@dataclass(frozen=True, slots=True)
class AppConfig:
    default_provider: str
    providers: Mapping[str, ProviderConfig]
    api_key: str = field(repr=False)

    @property
    def active_provider(self) -> ProviderConfig:
        return self.providers[self.default_provider]


def _expect_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{path} 必须是映射")
    return cast(Mapping[str, Any], value)


def _validate_exact_keys(data: Mapping[str, Any], expected: set[str], path: str) -> None:
    actual = set(data)
    missing = sorted(expected - actual)
    extra = sorted((str(key) for key in actual - expected))
    if missing:
        raise ConfigError(f"{path} 缺少字段: {', '.join(missing)}")
    if extra:
        raise ConfigError(f"{path} 包含未知字段: {', '.join(extra)}")


def _parse_provider(provider_id: str, raw: Any) -> ProviderConfig:
    path = f"providers.{provider_id}"
    data = _expect_mapping(raw, path)
    _validate_exact_keys(data, _PROVIDER_KEYS, path)

    expected = _EXPECTED_PROVIDERS[provider_id]
    for key, expected_value in expected.items():
        value = data[key]
        if key == "max_tokens":
            if type(value) is not int:
                raise ConfigError(f"{path}.max_tokens 必须是整数")
        elif not isinstance(value, str):
            raise ConfigError(f"{path}.{key} 必须是字符串")
        if value != expected_value:
            raise ConfigError(f"{path}.{key} 必须为 {expected_value}")

    return ProviderConfig(
        provider_id=provider_id,
        protocol=cast(ProtocolName, data["protocol"]),
        base_url=cast(str, data["base_url"]),
        api_key_env=cast(str, data["api_key_env"]),
        model=cast(str, data["model"]),
        max_tokens=cast(int, data["max_tokens"]),
    )


def load_config(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load the exact Chapter 01 schema and its active API key."""

    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"无法读取配置文件: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是有效 YAML: {path}") from exc

    data = _expect_mapping(raw, "配置根节点")
    _validate_exact_keys(data, _TOP_LEVEL_KEYS, "配置根节点")

    default_provider = data["default_provider"]
    if not isinstance(default_provider, str):
        raise ConfigError("default_provider 必须是字符串")

    raw_providers = _expect_mapping(data["providers"], "providers")
    actual_provider_ids = set(raw_providers)
    expected_provider_ids = set(_EXPECTED_PROVIDERS)
    if actual_provider_ids != expected_provider_ids:
        missing = sorted(expected_provider_ids - actual_provider_ids)
        extra = sorted(actual_provider_ids - expected_provider_ids)
        details: list[str] = []
        if missing:
            details.append(f"缺少 {', '.join(missing)}")
        if extra:
            details.append(f"包含未知项 {', '.join(extra)}")
        raise ConfigError(f"providers 必须准确包含两个 Provider（{'；'.join(details)}）")

    providers = {
        provider_id: _parse_provider(provider_id, raw_providers[provider_id])
        for provider_id in _EXPECTED_PROVIDERS
    }
    if default_provider not in providers:
        raise ConfigError(f"default_provider 不存在: {default_provider}")

    active_provider = providers[default_provider]
    environment = os.environ if environ is None else environ
    api_key = environment.get(active_provider.api_key_env, "")
    if not api_key.strip():
        raise ConfigError(f"环境变量 {active_provider.api_key_env} 缺失或为空")

    return AppConfig(
        default_provider=default_provider,
        providers=providers,
        api_key=api_key,
    )
