"""Create the configured provider adapter."""

from mewcode_agent.config import ProviderConfig
from mewcode_agent.providers.anthropic_provider import AnthropicProvider
from mewcode_agent.providers.base import LLMProvider, ProviderError
from mewcode_agent.providers.openai_provider import OpenAIProvider


def create_provider(config: ProviderConfig, api_key: str) -> LLMProvider:
    if config.protocol == "openai":
        return OpenAIProvider(config, api_key)
    if config.protocol == "anthropic":
        return AnthropicProvider(config, api_key)
    raise ProviderError(f"不支持的协议: {config.protocol}")
