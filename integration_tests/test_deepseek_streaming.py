from __future__ import annotations

import os
from pathlib import Path

import pytest

from mewcode_agent.config import load_config
from mewcode_agent.models import ChatMessage
from mewcode_agent.providers.factory import create_provider

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["deepseek_openai", "deepseek_anthropic"])
async def test_real_deepseek_streaming(provider_id: str) -> None:
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        pytest.skip("DEEPSEEK_API_KEY 未设置")

    config = load_config(Path.cwd() / "llm_providers.yaml")
    provider_config = config.providers[provider_id]
    provider = create_provider(provider_config, config.api_key)

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [ChatMessage(role="user", content="只回复 OK")]
        )
    ]

    assert "".join(chunks).strip()
