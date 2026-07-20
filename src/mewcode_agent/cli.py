"""Console entry point."""

from __future__ import annotations

import asyncio
import json
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
from mewcode_agent.hooks import (
    HookActionRunner,
    HookConfigError,
    HookDiagnostic,
    HookEngine,
    HookLifecycle,
    HookToolExecutionInterceptor,
    PromptHookBridge,
    load_hook_configuration,
)
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
from mewcode_agent.workers import (
    HookSubagentLauncher,
    SpawnWorkerTool,
    WorkerCatalog,
    WorkerCommandManager,
    WorkerConfigError,
    WorkerDiagnostic,
    WorkerExecutor,
    WorkerManager,
    builtin_worker_root,
    scan_worker_catalog,
)
from mewcode_agent.worktrees import (
    WorktreeConfigError,
    WorktreeManager,
    load_worktree_config,
)

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


def _print_hook_diagnostic(diagnostic: HookDiagnostic) -> None:
    source = diagnostic.source if diagnostic.source is not None else "runtime"
    rule_id = diagnostic.rule_id if diagnostic.rule_id is not None else "none"
    action = (
        diagnostic.action_type
        if diagnostic.action_type is not None
        else "none"
    )
    print(
        "Hook 警告："
        f"source={source} "
        f"rule={rule_id} "
        f"event={diagnostic.event} "
        f"action={action} "
        f"code={diagnostic.code} "
        f"{diagnostic.message}",
        file=sys.stderr,
    )


def _print_worker_diagnostic(diagnostic: WorkerDiagnostic) -> None:
    print(
        "Worker 警告："
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
    hook_action_runner: HookActionRunner | None = None
    hook_engine: HookEngine | None = None
    hook_lifecycle: HookLifecycle | None = None
    hook_lifecycle_started = False
    worker_manager: WorkerManager | None = None
    worktree_manager: WorktreeManager | None = None
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
        worktree_configuration = load_worktree_config(
            user_config_directory / "worktrees.yaml"
        )
        worktree_manager = await WorktreeManager.open(
            working_directory,
            worktree_configuration,
        )
        if worktree_manager.available:
            worktree_manager.start_cleanup()
        project_prompt_path = (
            working_directory / ".mewcode" / "prompts.yaml"
        )
        user_security_path = user_config_directory / "security.yaml"
        user_hook_path = user_config_directory / "hooks.yaml"
        mcp_config_path = user_config_directory / "mcp_servers.yaml"
        project_security_path = (
            working_directory / ".mewcode" / "security.yaml"
        )
        project_hook_path = working_directory / ".mewcode" / "hooks.yaml"
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
        hook_configuration = load_hook_configuration(
            user_path=user_hook_path,
            project_path=project_hook_path,
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
            (*BUILTIN_COMMAND_KEYS, "skills", "workers", "worker")
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
        builtin_workers = builtin_worker_root()
        worker_snapshot = scan_worker_catalog(
            project_root=working_directory,
            user_root=user_config_directory,
            builtin_root=builtin_workers,
            existing_tool_names=registry.tool_names(),
            provider_ids=config.providers,
            diagnostic_handler=_print_worker_diagnostic,
        )
        worker_catalog = WorkerCatalog(worker_snapshot)
        security_boundary = registry.security_boundary
        assert security_boundary is not None
        security_policy = SecurityPolicyEngine(
            security_configuration,
            security_boundary,
            approval_store=approval_store,
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
        prompt_hook_bridge = PromptHookBridge(
            prompt_runtime,
            history_length_provider=lambda: len(history.snapshot()),
        )
        hook_action_runner = HookActionRunner(
            project_root=working_directory,
            prompt_sink=prompt_hook_bridge,
        )
        hook_engine = HookEngine(
            hook_configuration,
            hook_action_runner,
            project_root=working_directory,
            session_id_provider=lambda: session_manager.active_session_id,
            diagnostic_handler=_print_hook_diagnostic,
        )
        provider_cache = {provider_config.provider_id: provider}

        def resolve_worker_provider(provider_id: str):
            cached = provider_cache.get(provider_id)
            if cached is None:
                cached = create_provider(
                    config.providers[provider_id],
                    config.api_key,
                )
                provider_cache[provider_id] = cached
            return cached

        def create_worker_policy(permission_mode: str):
            policy = SecurityPolicyEngine(
                security_configuration,
                security_boundary,
                approval_store=approval_store,
            )
            if permission_mode != "inherit":
                policy.set_mode_override(permission_mode)
            return policy

        def create_worker_context_manager(worker_provider):
            worker_provider_config = config.providers[
                worker_provider.provider_id
            ]
            return ContextWindowManager(
                worker_provider,
                ToolResultCompactor(
                    artifact_store,
                    config=compaction_config,
                ),
                ContextSummarizer(
                    worker_provider,
                    timeout_seconds=loop_config.llm_timeout_seconds,
                    config=compaction_config,
                ),
                context_window_tokens=(
                    worker_provider_config.context_window_tokens
                ),
                max_tokens=worker_provider_config.max_tokens,
                config=compaction_config,
            )

        worker_executor = WorkerExecutor(
            registry=registry,
            parent_prompt_runtime=prompt_runtime,
            prompt_composer=prompt_composer,
            provider_resolver=resolve_worker_provider,
            policy_engine_factory=create_worker_policy,
            context_manager_factory=create_worker_context_manager,
            hook_engine=hook_engine,
            hook_action_runner=hook_action_runner,
            prompt_hook_bridge=prompt_hook_bridge,
            skill_runtime=skill_runtime,
            load_skill_tool=load_skill_tool,
            worktree_manager=worktree_manager,
        )
        worker_manager = WorkerManager(
            worker_snapshot.runtime_config,
            worker_executor.run,
            cancel_runner=worker_executor.cancel,
            workspace_provider=worker_executor.workspace_snapshot,
        )
        provider_models = {
            provider_id: item.model
            for provider_id, item in config.providers.items()
        }
        spawn_worker_tool = SpawnWorkerTool(
            catalog=worker_catalog,
            manager=worker_manager,
            registry=registry,
            main_history=history,
            session_id_provider=lambda: session_manager.active_session_id,
            parent_visible_tools=skill_runtime.visible_tool_names,
            parent_provider_id=provider_config.provider_id,
            provider_models=provider_models,
        )
        registry.register(spawn_worker_tool)
        hook_subagent_launcher = HookSubagentLauncher(
            catalog=worker_catalog,
            manager=worker_manager,
            registry=registry,
            main_history=history,
            session_id_provider=lambda: session_manager.active_session_id,
            parent_visible_tools=skill_runtime.visible_tool_names,
            parent_provider_id=provider_config.provider_id,
            provider_models=provider_models,
            summarizer=context_summarizer,
        )
        hook_action_runner.set_subagent_runner(
            hook_subagent_launcher.launch
        )
        scheduler = ToolScheduler(
            registry,
            interceptor=HookToolExecutionInterceptor(hook_engine),
            policy_engine=security_policy,
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
        HookConfigError,
        WorkerConfigError,
        WorktreeConfigError,
    ) as exc:
        try:
            if worker_manager is not None:
                await worker_manager.close()
        finally:
            try:
                if worktree_manager is not None:
                    await worktree_manager.close()
            finally:
                try:
                    if hook_engine is not None:
                        await hook_engine.close()
                    elif hook_action_runner is not None:
                        await hook_action_runner.close()
                finally:
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
        assert hook_engine is not None
        assert hook_action_runner is not None
        assert worker_manager is not None
        assert worktree_manager is not None

        async def worker_request_controls() -> tuple[
            RuntimeInstruction, ...
        ]:
            notifications = await worker_manager.take_notifications(
                session_manager.active_session_id
            )
            return tuple(
                RuntimeInstruction(
                    f"runtime.workers.notification_{item.task_id}",
                    "context",
                    "request",
                    json.dumps(
                        item.to_dict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "worker",
                )
                for item in notifications
            )

        agent_loop = AgentLoop(
            provider,
            registry,
            prompt_runtime=prompt_runtime,
            prompt_composer=prompt_composer,
            scheduler=scheduler,
            context_window_manager=context_window_manager,
            visible_tool_names=skill_runtime.visible_tool_names,
            hook_engine=hook_engine,
            request_control_provider=worker_request_controls,
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
        hook_lifecycle = HookLifecycle(
            hook_engine,
            active_session_id=lambda: session_manager.active_session_id,
        )

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
        worker_command_manager = WorkerCommandManager(
            worker_catalog,
            worker_manager,
        )

        async def session_switched(
            previous: str,
            restored: bool,
        ) -> None:
            await worker_manager.clear_notifications(previous)
            await hook_lifecycle.session_switched(
                previous,
                restored=restored,
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
                session_switched,
            ),
            extra_specs=(
                skill_command_manager.management_spec(),
                *worker_command_manager.specs(),
            ),
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
            worker_manager=worker_manager,
        )
        await hook_lifecycle.start()
        hook_lifecycle_started = True
        await app.run_async()
        return 0
    finally:
        assert mcp_manager is not None
        assert artifact_store is not None
        assert session_manager is not None
        assert notes_manager is not None
        assert hook_action_runner is not None
        assert hook_engine is not None
        assert worker_manager is not None
        assert worktree_manager is not None
        try:
            await worker_manager.close()
        finally:
            try:
                await worktree_manager.close()
            finally:
                try:
                    await notes_manager.flush_on_exit()
                finally:
                    try:
                        if hook_lifecycle_started and hook_lifecycle is not None:
                            await hook_lifecycle.end_active_session()
                        await hook_engine.close()
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
