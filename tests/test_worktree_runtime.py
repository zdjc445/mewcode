from __future__ import annotations

from pathlib import Path

from mewcode_agent.hooks import (
    HookActionRunner,
    HookConfiguration,
    HookEngine,
)
from mewcode_agent.prompting import (
    GitEnvironment,
    PromptRuntime,
    RequestEnvironment,
    SessionEnvironment,
)
from mewcode_agent.security import PathSandbox, PathSandboxError
from mewcode_agent.worktrees import (
    bind_worktree_runtime,
    fork_worktree_prompt_runtime,
    worktree_worker_control,
)


class _Collector:
    async def collect(self) -> RequestEnvironment:
        return RequestEnvironment(
            "2026-07-21T12:00:00+08:00",
            GitEnvironment("not_repository", None, None, None),
        )


class _Sink:
    async def inject(self, *_args, **_kwargs) -> None:
        return None

    async def flush(self) -> tuple[str, ...]:
        return ()

    def reset_session(self, **_kwargs) -> int:
        return 0

    def discard_pending(self) -> int:
        return 0


def _runtime(root: Path) -> PromptRuntime:
    return PromptRuntime(
        SessionEnvironment(
            "Windows",
            "powershell.exe",
            str(root),
            "UTC",
            "+00:00",
        ),
        _Collector(),
    )


async def test_runtime_binding_moves_all_shared_roots_and_resets(
    tmp_path: Path,
) -> None:
    main = (tmp_path / "main").resolve()
    isolated = (main / ".mewcode" / ".worktrees" / "worker").resolve()
    isolated.mkdir(parents=True)
    sandbox = PathSandbox(main)
    action_runner = HookActionRunner(
        project_root=main,
        prompt_sink=_Sink(),  # type: ignore[arg-type]
    )
    engine = HookEngine(
        HookConfiguration(()),
        action_runner,
        project_root=main,
    )

    with bind_worktree_runtime(
        isolated,
        path_sandbox=sandbox,
        hook_action_runner=action_runner,
        hook_engine=engine,
    ):
        assert sandbox.working_directory == isolated
        assert action_runner.project_root == isolated
        assert engine.project_root == isolated
        try:
            sandbox.resolve(main / "outside.txt")
        except PathSandboxError:
            pass
        else:
            raise AssertionError("bound sandbox accepted main root")

    assert sandbox.working_directory == main
    assert action_runner.project_root == main
    assert engine.project_root == main
    await engine.close()


def test_worktree_prompt_runtime_replaces_cwd_and_injects_boundary(
    tmp_path: Path,
) -> None:
    main = (tmp_path / "main").resolve()
    isolated = (main / ".mewcode" / ".worktrees" / "worker").resolve()
    isolated.mkdir(parents=True)
    control = worktree_worker_control(
        task_id="a" * 32,
        main_root=main,
        worktree_root=isolated,
    )

    forked = fork_worktree_prompt_runtime(
        _runtime(main),
        worktree_root=isolated,
        extra_controls=(control,),
    )

    timeline = forked.timeline()
    assert str(isolated).replace("\\", "\\\\") in timeline[0].content
    assert str(main) in timeline[1].content
    assert str(isolated) in timeline[1].content
    assert "Do not modify the main repository root" in timeline[1].content
