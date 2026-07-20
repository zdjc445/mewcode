from mewcode_agent.config import ProviderConfig
from mewcode_agent.providers.anthropic_provider import AnthropicProvider
from mewcode_agent.providers.factory import create_provider
from mewcode_agent.providers.openai_provider import OpenAIProvider
from mewcode_agent.providers.base import ProviderError
import pytest


def test_factory_creates_openai_provider(openai_config: ProviderConfig) -> None:
    provider = create_provider(openai_config, "test-secret")

    assert isinstance(provider, OpenAIProvider)


def test_factory_creates_anthropic_provider(anthropic_config: ProviderConfig) -> None:
    provider = create_provider(anthropic_config, "test-secret")

    assert isinstance(provider, AnthropicProvider)


def test_factory_rejects_unknown_protocol(openai_config: ProviderConfig) -> None:
    invalid = ProviderConfig(
        provider_id=openai_config.provider_id,
        protocol="other",  # type: ignore[arg-type]
        base_url=openai_config.base_url,
        api_key_env=openai_config.api_key_env,
        model=openai_config.model,
        max_tokens=openai_config.max_tokens,
        context_window_tokens=openai_config.context_window_tokens,
    )

    with pytest.raises(ProviderError, match="不支持的协议: other"):
        create_provider(invalid, "test-secret")
