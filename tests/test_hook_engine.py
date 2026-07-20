import asyncio
from pathlib import Path
from typing import Any

from mewcode_agent.hooks import (
    HookConfiguration,
    HookCondition,
    HookDiagnostic,
    HookEngine,
    HookInterception,
    HookRule,
    HookValueMatcher,
    ShellHookAction,
)


class FakeActionRunner:
    def __init__(self) -> None:
        self.prepared: list[str] = []
        self.executed: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.closed = 0

    def prepare(self, action: Any, context: dict[str, object]) -> str:
        self.prepared.append(action.command)
        return action.command

    async def execute(
        self,
        action: str,
        *,
        event_sequence: int,
        rule_id: str,
    ) -> None:
        self.executed.append(rule_id)
        gate = self.gates.get(rule_id)
        if gate is not None:
            await gate.wait()

    async def close(self) -> int:
        self.closed += 1
        return 2


def make_rule(
    rule_id: str,
    *,
    source: str = "project",
    once: bool = False,
    run_async: bool = False,
    command: str = "ok",
    interception: HookInterception | None = None,
) -> HookRule:
    return HookRule(
        rule_id,
        source,  # type: ignore[arg-type]
        "tool.before_execute",
        once,
        run_async,
        1,
        HookCondition(
            "all",
            {"tool.name": HookValueMatcher("exact", "write_file")},
        ),
        ShellHookAction(command),
        interception,
    )


async def test_engine_runs_in_order_and_once_is_process_lifetime(
    tmp_path: Path,
) -> None:
    runner = FakeActionRunner()
    engine = HookEngine(
        HookConfiguration(
            (
                make_rule("first", once=True),
                make_rule("second"),
            )
        ),
        runner,  # type: ignore[arg-type]
        project_root=tmp_path.resolve(),
        session_id_provider=lambda: "a" * 32,
    )

    await engine.dispatch("tool.before_execute", {"tool.name": "write_file"})
    await engine.dispatch("tool.before_execute", {"tool.name": "write_file"})
    close = await engine.close()

    assert runner.executed == ["first", "second", "second"]
    assert close.pending_prompts_discarded == 2
    assert runner.closed == 1


async def test_engine_project_root_binding_is_in_context_and_resets(
    tmp_path: Path,
) -> None:
    default = (tmp_path / "default").resolve()
    bound = (tmp_path / "bound").resolve()
    default.mkdir()
    bound.mkdir()

    class ContextRunner(FakeActionRunner):
        def __init__(self) -> None:
            super().__init__()
            self.roots: list[str] = []

        def prepare(self, action: Any, context: dict[str, object]) -> str:
            self.roots.append(str(context["project.root"]))
            return super().prepare(action, context)

    runner = ContextRunner()
    engine = HookEngine(
        HookConfiguration((make_rule("root"),)),
        runner,  # type: ignore[arg-type]
        project_root=default,
    )

    with engine.bind_project_root(bound):
        assert engine.project_root == bound
        await engine.dispatch(
            "tool.before_execute",
            {"tool.name": "write_file"},
        )
    assert engine.project_root == default
    await engine.dispatch(
        "tool.before_execute",
        {"tool.name": "write_file"},
    )
    await engine.close()

    assert runner.roots == [str(bound), str(default)]


async def test_background_hook_binding_is_inherited_and_drainable(
    tmp_path: Path,
) -> None:
    default = (tmp_path / "default").resolve()
    bound = (tmp_path / "bound").resolve()
    default.mkdir()
    bound.mkdir()

    class ContextRunner(FakeActionRunner):
        def __init__(self) -> None:
            super().__init__()
            self.roots: list[str] = []

        def prepare(self, action: Any, context: dict[str, object]) -> str:
            self.roots.append(str(context["project.root"]))
            return super().prepare(action, context)

    runner = ContextRunner()
    runner.gates["background"] = asyncio.Event()
    engine = HookEngine(
        HookConfiguration((make_rule("background", run_async=True),)),
        runner,  # type: ignore[arg-type]
        project_root=default,
    )

    with engine.bind_project_root(bound):
        await engine.dispatch(
            "tool.before_execute",
            {"tool.name": "write_file"},
        )
    await asyncio.sleep(0)
    drain = asyncio.create_task(engine.drain_project_root(bound))
    await asyncio.sleep(0)

    assert not drain.done()
    runner.gates["background"].set()
    assert await drain == 1
    assert runner.roots == [str(bound)]
    await engine.close()


async def test_engine_background_task_is_drained_on_close(
    tmp_path: Path,
) -> None:
    runner = FakeActionRunner()
    runner.gates["background"] = asyncio.Event()
    engine = HookEngine(
        HookConfiguration((make_rule("background", run_async=True),)),
        runner,  # type: ignore[arg-type]
        project_root=tmp_path.resolve(),
    )

    await engine.dispatch("tool.before_execute", {"tool.name": "write_file"})
    await asyncio.sleep(0)
    close_task = asyncio.create_task(engine.close())
    await asyncio.sleep(0)
    assert not close_task.done()
    runner.gates["background"].set()
    result = await close_task

    assert result.background_tasks_waited == 1
    assert runner.executed == ["background"]


async def test_first_interception_stops_later_before_rules(
    tmp_path: Path,
) -> None:
    runner = FakeActionRunner()
    engine = HookEngine(
        HookConfiguration(
            (
                make_rule(
                    "deny",
                    interception=HookInterception(
                        True,
                        "blocked ${tool.name}",
                    ),
                ),
                make_rule("never"),
            )
        ),
        runner,  # type: ignore[arg-type]
        project_root=tmp_path.resolve(),
    )

    result = await engine.dispatch(
        "tool.before_execute",
        {"tool.name": "write_file"},
    )
    await engine.close()

    assert result.blocked
    assert result.block_reason == "blocked write_file"
    assert runner.executed == ["deny"]


async def test_invalid_context_and_action_failures_only_report_diagnostics(
    tmp_path: Path,
) -> None:
    diagnostics: list[HookDiagnostic] = []
    runner = FakeActionRunner()
    engine = HookEngine(
        HookConfiguration((make_rule("rule"),)),
        runner,  # type: ignore[arg-type]
        project_root=tmp_path.resolve(),
        diagnostic_handler=diagnostics.append,
    )

    result = await engine.dispatch(
        "tool.before_execute",
        {"Bad.Path": "value"},
    )
    await engine.close()

    assert not result.blocked
    assert diagnostics[0].code == "hook_context_invalid"


async def test_close_dispatches_shutdown_once_and_is_idempotent(
    tmp_path: Path,
) -> None:
    runner = FakeActionRunner()
    shutdown_rule = HookRule(
        "shutdown",
        "user",
        "system.shutdown",
        False,
        False,
        1,
        None,
        ShellHookAction("shutdown"),
        None,
    )
    engine = HookEngine(
        HookConfiguration((shutdown_rule,)),
        runner,  # type: ignore[arg-type]
        project_root=tmp_path.resolve(),
    )

    first = await engine.close()
    second = await engine.close()

    assert runner.executed == ["shutdown"]
    assert runner.closed == 1
    assert first == second
