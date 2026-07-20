"""Console entry point."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mewcode_agent.agent import AgentLoop, AgentLoopConfig, ToolScheduler
from mewcode_agent.app import ChatApp
from mewcode_agent.compaction import (
    CompactionConfig,
    ContextArtifactStore,
    ContextCompactionError,
    ContextSummarizer,
    ContextWindowManager,
    ToolResultCompactor,
)
from mewcode_agent.commands import (
    BUILTIN_COMMAND_KEYS,
    BuiltinCommandServices,
    PermissionCommandPaths,
    build_builtin_command_registry,
)
from mewcode_agent.config import ConfigError, load_config
from mewcode_agent.history import ConversationHistory
from mewcode_agent.instructions import (
    InstructionConfigError,
    load_instruction_documents,
)
from mewcode_agent.mcp import (
    McpConnectionManager,
    McpDiagnostic,
    McpError,
    load_mcp_config,
)
from mewcode_agent.notes import (
    NoteUpdater,
    NoteWarning,
    NotesError,
    NotesManager,
    load_notes,
    note_paths,
)
from mewcode_agent.prompting import (
    GitRequestEnvironmentCollector,
    PromptComposer,
    PromptConfigError,
    PromptEnvironmentError,
    PromptRuntime,
    RuntimeInstruction,
    collect_session_environment,
    load_prompt_modules,
)
from mewcode_agent.providers.base import ProviderError
from mewcode_agent.providers.factory import create_provider
from mewcode_agent.security import (
    PathSandboxError,
    PermanentApprovalStore,
    SecurityConfigError,
    SecurityPolicyEngine,
    load_security_configuration,
)
from mewcode_agent.sessions import (
    SessionError,
    SessionManager,
    SessionRecovery,
)
from mewcode_agent.skills import (
    IsolatedSkillExecutor,
    LoadSkillTool,
    SkillCatalog,
    SkillCommandManager,
    SkillConfigError,
    SkillDiagnostic,
    SkillRuntime,
    builtin_skill_root,
    reject_isolated_approval,
    scan_skill_catalog,
)
from mewcode_agent.tools.registry import create_core_registry

CONFIG_FILENAME = "llm_providers.yaml"


def _print_mcp_diagnostic(diagnostic: McpDiagnostic) -> None:
    print(
        "MCP 警告："
        f"server={diagnostic.server_id} "
        f"code={diagnostic.code} "
        f"{diagnostic.message}",
        file=sys.stderr,
    )


def _print_note_warning(warning: NoteWarning) -> None:
    scope = warning.scope if warning.scope is not None else "all"
    print(
        f"笔记警告：scope={scope} code={warning.code}",
        file=sys.stderr,
    )


def _print_skill_diagnostic(diagnostic: SkillDiagnostic) -> None:
    print(
        "Skill 警告："
        f"source={diagnostic.source} "
        f"candidate={diagnostic.candidate} "
        f"code={diagnostic.code} "
        f"{diagnostic.message}",
        file=sys.stderr,
    )


async def run_application() -> int:
    """Build and run one application session on the current event loop."""

    mcp_manager: McpConnectionManager | None = None
    artifact_store: ContextArtifactStore | None = None
    session_manager: SessionManager | None = None
    notes_manager: NotesManager | None = None
    try:
        session_environment = collect_session_environment()
        working_directory = Path(
            session_environment.working_directory
        )
        config_path = working_directory / CONFIG_FILENAME
        try:
            user_config_directory = Path.home() / ".mewcode-agent"
        except (OSError, RuntimeError) as exc:
            raise PromptConfigError(
                "无法解析用户全局 Prompt 配置路径"
            ) from exc
        user_prompt_path = user_config_directory / "prompts.yaml"
        project_prompt_path = (
            working_directory / ".mewcode" / "prompts.yaml"
        )
        user_security_path = user_config_directory / "security.yaml"
        mcp_config_path = user_config_directory / "mcp_servers.yaml"
        project_security_path = (
            working_directory / ".mewcode" / "security.yaml"
        )
        permanent_approval_path = (
            user_config_directory / "security-approvals.yaml"
        )
        approval_store = PermanentApprovalStore(permanent_approval_path)
        config = load_config(config_path)
        modules = load_prompt_modules(
            user_path=user_prompt_path,
            project_path=project_prompt_path,
        )
        instruction_documents = load_instruction_documents(
            user_root=user_config_directory,
            project_root=working_directory,
        )
        paths = note_paths(
            user_root=user_config_directory,
            project_root=working_directory,
        )
        initial_notes = load_notes(paths=paths)
        environment_collector = GitRequestEnvironmentCollector(
            working_directory=Path(
                session_environment.working_directory
            )
        )
        prompt_runtime = PromptRuntime(
            session_environment,
            environment_collector,
            session_controls=tuple(
                document.to_runtime_instruction()
                for document in instruction_documents
            ) + initial_notes.runtime_controls(generation=1),
        )
        prompt_composer = PromptComposer(modules)
        permanent_rules = approval_store.load()
        security_configuration = load_security_configuration(
            user_path=user_security_path,
            project_path=project_security_path,
            permanent_rules=permanent_rules,
        )
        provider_config = config.active_provider
        history = ConversationHistory()
        session_manager = SessionManager(
            sessions_root=user_config_directory / "sessions",
            project_root=working_directory,
            provider_id=provider_config.provider_id,
            model=provider_config.model,
            history=history,
        )
        provider = create_provider(provider_config, config.api_key)
        notes_manager = NotesManager(
            NoteUpdater(
                provider,
                project_root=working_directory,
                timeout_seconds=120.0,
            ),
            paths=paths,
            initial_snapshot=initial_notes,
            history=history,
            prompt_runtime=prompt_runtime,
            warning_handler=_print_note_warning,
        )
        compaction_config = CompactionConfig()
        artifact_store = ContextArtifactStore(
            root=user_config_directory / "context-artifacts",
            config=compaction_config,
        )
        await artifact_store.cleanup_stale()
        await artifact_store.initialize()
        registry = create_core_registry(
            working_directory=working_directory,
            artifact_store=artifact_store,
        )
        mcp_configuration = load_mcp_config(
            working_directory=working_directory,
            path=mcp_config_path,
        )
        mcp_manager = McpConnectionManager(
            mcp_configuration,
            registry,
            diagnostic_handler=_print_mcp_diagnostic,
        )
        await mcp_manager.activate_all()
        reserved_command_names = frozenset(
            (*BUILTIN_COMMAND_KEYS, "skills")
        )
        builtin_skills = builtin_skill_root()
        skill_snapshot = scan_skill_catalog(
            project_root=working_directory,
            user_root=user_config_directory,
            builtin_root=builtin_skills,
            existing_tool_names=registry.tool_names(),
            reserved_command_names=reserved_command_names,
            diagnostic_handler=_print_skill_diagnostic,
        )
        skill_runtime = SkillRuntime(
            SkillCatalog(skill_snapshot),
            registry,
            prompt_runtime,
            reserved_command_names=reserved_command_names,
        )
        load_skill_tool = LoadSkillTool(skill_runtime)
        registry.register(load_skill_tool)
        security_boundary = registry.security_boundary
        assert security_boundary is not None
        security_policy = SecurityPolicyEngine(
            security_configuration,
            security_boundary,
            approval_store=approval_store,
        )
        scheduler = ToolScheduler(
            registry,
            policy_engine=security_policy,
        )
        loop_config = AgentLoopConfig()
        context_summarizer = ContextSummarizer(
            provider,
            timeout_seconds=loop_config.llm_timeout_seconds,
            config=compaction_config,
        )
        context_window_manager = ContextWindowManager(
            provider,
            ToolResultCompactor(
                artifact_store,
                config=compaction_config,
            ),
            context_summarizer,
            context_window_tokens=provider_config.context_window_tokens,
            max_tokens=provider_config.max_tokens,
            config=compaction_config,
        )
    except (
        ConfigError,
        SecurityConfigError,
        InstructionConfigError,
        PromptConfigError,
        PromptEnvironmentError,
        PathSandboxError,
        ProviderError,
        McpError,
        ContextCompactionError,
        SessionError,
        NotesError,
        SkillConfigError,
    ) as exc:
        try:
            if session_manager is not None:
                session_manager.close()
        finally:
            try:
                if mcp_manager is not None:
                    await mcp_manager.close()
            finally:
                if artifact_store is not None:
                    await artifact_store.close()
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    try:
        agent_loop = AgentLoop(
            provider,
            registry,
            prompt_runtime=prompt_runtime,
            prompt_composer=prompt_composer,
            scheduler=scheduler,
            context_window_manager=context_window_manager,
            visible_tool_names=skill_runtime.visible_tool_names,
        )
        isolated_executor = IsolatedSkillExecutor(
            provider=provider,
            registry=registry,
            scheduler=scheduler,
            prompt_runtime=prompt_runtime,
            prompt_composer=prompt_composer,
            skill_runtime=skill_runtime,
            load_skill_tool=load_skill_tool,
            main_history=history,
            summarizer=context_summarizer,
            approval_handler=reject_isolated_approval,
            loop_config=loop_config,
        )
        skill_runtime.set_isolated_runner(isolated_executor.run)
        assert session_manager is not None
        assert notes_manager is not None

        def load_current_session_controls() -> tuple[
            RuntimeInstruction, ...
        ]:
            documents = load_instruction_documents(
                user_root=user_config_directory,
                project_root=working_directory,
            )
            controls = tuple(
                document.to_runtime_instruction() for document in documents
            )
            controls = (*controls, *notes_manager.reload_for_session())
            return controls

        def activate_session(recovery: SessionRecovery) -> None:
            controls = load_current_session_controls()
            gap = session_manager.resume_gap_instruction(recovery.meta)
            if gap is not None:
                controls = (*controls, gap)
            agent_loop.reset_session(session_controls=controls)
            skill_runtime.reset_session()

        def activate_new_session() -> None:
            agent_loop.reset_session(
                session_controls=load_current_session_controls()
            )
            skill_runtime.reset_session()

        skill_command_manager = SkillCommandManager(
            project_root=working_directory,
            user_root=user_config_directory,
            builtin_root=builtin_skills,
            tool_registry=registry,
            skill_runtime=skill_runtime,
            reserved_command_names=reserved_command_names,
            diagnostic_handler=_print_skill_diagnostic,
        )

        command_registry = build_builtin_command_registry(
            BuiltinCommandServices(
                agent_loop,
                history,
                session_manager,
                notes_manager,
                security_policy,
                provider_config.provider_id,
                provider_config.model,
                PermissionCommandPaths(
                    user_security_path.resolve(strict=False),
                    project_security_path.resolve(strict=False),
                    permanent_approval_path.resolve(strict=False),
                ),
                activate_session,
                activate_new_session,
            ),
            extra_specs=(skill_command_manager.management_spec(),),
        )
        skill_command_manager.bind_registry(command_registry)
        command_registry.replace_dynamic(
            skill_command_manager.dynamic_specs()
        )

        app = ChatApp(
            agent_loop,
            history,
            provider_id=provider_config.provider_id,
            model=provider_config.model,
            command_registry=command_registry,
            notes_manager=notes_manager,
        )
        await app.run_async()
        return 0
    finally:
        assert mcp_manager is not None
        assert artifact_store is not None
        assert session_manager is not None
        assert notes_manager is not None
        try:
            await notes_manager.flush_on_exit()
        finally:
            try:
                session_manager.close()
            finally:
                try:
                    await mcp_manager.close()
                finally:
                    await artifact_store.close()


def main() -> int:
    try:
        return asyncio.run(run_application())
    except KeyboardInterrupt:
        return 130
