"""Console entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from mewcode_agent.agent import AgentLoop
from mewcode_agent.app import ChatApp
from mewcode_agent.config import ConfigError, load_config
from mewcode_agent.history import ConversationHistory
from mewcode_agent.prompting import (
    GitRequestEnvironmentCollector,
    PromptComposer,
    PromptConfigError,
    PromptEnvironmentError,
    PromptRuntime,
    collect_session_environment,
    load_prompt_modules,
)
from mewcode_agent.providers.base import ProviderError
from mewcode_agent.providers.factory import create_provider
from mewcode_agent.tools.registry import create_core_registry

CONFIG_FILENAME = "llm_providers.yaml"


def main() -> int:
    try:
        session_environment = collect_session_environment()
        working_directory = Path(
            session_environment.working_directory
        )
        config_path = working_directory / CONFIG_FILENAME
        try:
            user_prompt_path = (
                Path.home() / ".mewcode-agent" / "prompts.yaml"
            )
        except (OSError, RuntimeError) as exc:
            raise PromptConfigError(
                "无法解析用户全局 Prompt 配置路径"
            ) from exc
        project_prompt_path = (
            working_directory / ".mewcode" / "prompts.yaml"
        )
        config = load_config(config_path)
        modules = load_prompt_modules(
            user_path=user_prompt_path,
            project_path=project_prompt_path,
        )
        environment_collector = GitRequestEnvironmentCollector(
            working_directory=Path(
                session_environment.working_directory
            )
        )
        prompt_runtime = PromptRuntime(
            session_environment,
            environment_collector,
        )
        prompt_composer = PromptComposer(modules)
        provider_config = config.active_provider
        provider = create_provider(provider_config, config.api_key)
    except (
        ConfigError,
        PromptConfigError,
        PromptEnvironmentError,
        ProviderError,
    ) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    registry = create_core_registry()
    agent_loop = AgentLoop(
        provider,
        registry,
        prompt_runtime=prompt_runtime,
        prompt_composer=prompt_composer,
    )
    app = ChatApp(
        agent_loop,
        ConversationHistory(),
        provider_id=provider_config.provider_id,
        model=provider_config.model,
    )
    app.run()
    return 0
