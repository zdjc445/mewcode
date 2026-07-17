"""Console entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from mewcode_agent.app import ChatApp
from mewcode_agent.config import ConfigError, load_config
from mewcode_agent.history import ConversationHistory
from mewcode_agent.providers.base import ProviderError
from mewcode_agent.providers.factory import create_provider
from mewcode_agent.tools.registry import create_core_registry

CONFIG_FILENAME = "llm_providers.yaml"


def main() -> int:
    config_path = Path.cwd() / CONFIG_FILENAME
    try:
        config = load_config(config_path)
        provider_config = config.active_provider
        provider = create_provider(provider_config, config.api_key)
    except (ConfigError, ProviderError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    app = ChatApp(
        provider,
        ConversationHistory(),
        provider_id=provider_config.provider_id,
        model=provider_config.model,
        tool_registry=create_core_registry(),
    )
    app.run()
    return 0
