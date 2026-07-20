"""Context-local runtime bindings for a managed worktree worker."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from mewcode_agent.hooks import HookActionRunner, HookEngine
from mewcode_agent.prompting import (
    GitRequestEnvironmentCollector,
    PromptRuntime,
    RuntimeInstruction,
    collect_session_environment,
)
from mewcode_agent.security import PathSandbox
from mewcode_agent.worktrees.models import validate_task_id


def worktree_worker_control(
    *,
    task_id: str,
    main_root: Path,
    worktree_root: Path,
) -> RuntimeInstruction:
    validate_task_id(task_id)
    main = main_root.resolve(strict=True)
    isolated = worktree_root.resolve(strict=True)
    return RuntimeInstruction(
        f"runtime.workers.worktree_{task_id}",
        "context",
        "session",
        (
            "Managed Git worktree isolation is active.\n"
            f"Main repository root: {main}\n"
            f"Worker worktree root: {isolated}\n"
            "Resolve every relative path from the worker worktree root. "
            "Do not modify the main repository root."
        ),
        "worktree",
    )


def fork_worktree_prompt_runtime(
    parent: PromptRuntime,
    *,
    worktree_root: Path,
    extra_controls: tuple[RuntimeInstruction, ...],
) -> PromptRuntime:
    root = worktree_root.resolve(strict=True)
    return parent.fork_current_session(
        extra_controls=extra_controls,
        session_environment=collect_session_environment(
            working_directory=root
        ),
        request_environment_collector=GitRequestEnvironmentCollector(
            working_directory=root
        ),
    )


@contextmanager
def bind_worktree_runtime(
    path: Path,
    *,
    path_sandbox: PathSandbox,
    hook_action_runner: HookActionRunner | None,
    hook_engine: HookEngine | None,
) -> Iterator[Path]:
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("worktree runtime root 不是目录")
    with ExitStack() as stack:
        stack.enter_context(path_sandbox.bind_working_directory(root))
        if hook_action_runner is not None:
            stack.enter_context(hook_action_runner.bind_project_root(root))
        if hook_engine is not None:
            stack.enter_context(hook_engine.bind_project_root(root))
        yield root
