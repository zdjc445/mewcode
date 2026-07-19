from __future__ import annotations

import os
from pathlib import Path

import pytest

from mewcode_agent.config import load_config
from mewcode_agent.models import ChatMessage
from mewcode_agent.providers.base import (
    ProviderRequest,
    ProviderTextDelta,
    ProviderTurnEnd,
    ProviderUsageEvent,
)
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

    request = ProviderRequest(
        system_prompt="你是集成测试助手。",
        items=(ChatMessage(role="user", content="只回复 OK"),),
        tools=None,
    )
    events = [event async for event in provider.stream_chat(request)]
    text = "".join(
        event.text
        for event in events
        if isinstance(event, ProviderTextDelta)
    )
    usage_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, ProviderUsageEvent)
    )
    assert text.strip()
    assert isinstance(events[-1], ProviderTurnEnd)
    assert usage_index == len(events) - 2
