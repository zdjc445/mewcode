import asyncio
from pathlib import Path

import httpx
import pytest

from mewcode_agent.hooks import (
    HookActionError,
    HookActionRunner,
    HttpHookAction,
    PromptHookAction,
    ShellHookAction,
    SubagentHookAction,
)


class RecordingPromptSink:
    def __init__(self) -> None:
        self.items: list[tuple[str, int, str]] = []
        self.pending = 0

    async def inject(
        self,
        content: str,
        *,
        event_sequence: int,
        rule_id: str,
    ) -> None:
        self.items.append((content, event_sequence, rule_id))

    async def flush(self) -> tuple[str, ...]:
        return ()

    def discard_pending(self) -> int:
        value = self.pending
        self.pending = 0
        return value

    def reset_session(
        self,
        *,
        preserve_rule_ids: frozenset[str],
    ) -> int:
        del preserve_rule_ids
        return 0


async def test_prompt_and_http_actions_use_rendered_context(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    sink = RecordingPromptSink()
    runner = HookActionRunner(
        project_root=tmp_path.resolve(),
        prompt_sink=sink,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            follow_redirects=False,
        ),
    )
    context = {"event.name": "round.started", "event.sequence": 2}

    prompt = runner.prepare(
        PromptHookAction("event=${event.name}"),
        context,
    )
    await runner.execute(prompt, event_sequence=2, rule_id="prompt")
    http = runner.prepare(
        HttpHookAction(
            "POST",
            "https://example.test/${event.sequence}",
            {"X-Event": "${event.name}"},
            "${event.sequence}",
        ),
        context,
    )
    await runner.execute(http, event_sequence=2, rule_id="http")
    await runner.close()

    assert sink.items == [("event=round.started", 2, "prompt")]
    assert requests[0].url == httpx.URL("https://example.test/2")
    assert requests[0].headers["X-Event"] == "round.started"
    assert requests[0].content == b"2"


async def test_http_non_2xx_and_subagent_are_stable_errors(
    tmp_path: Path,
) -> None:
    runner = HookActionRunner(
        project_root=tmp_path.resolve(),
        prompt_sink=RecordingPromptSink(),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(302, headers={"Location": "/x"})
            )
        ),
    )
    http = runner.prepare(
        HttpHookAction("GET", "https://example.test", {}, ""),
        {},
    )
    with pytest.raises(HookActionError) as http_error:
        await runner.execute(http, event_sequence=1, rule_id="http")
    subagent = runner.prepare(
        SubagentHookAction("inspect", "none"),
        {},
    )
    with pytest.raises(HookActionError) as subagent_error:
        await runner.execute(subagent, event_sequence=2, rule_id="subagent")
    await runner.close()

    assert http_error.value.code == "hook_http_failed"
    assert subagent_error.value.code == "hook_subagent_unavailable"


async def test_shell_success_and_failure_are_isolated(tmp_path: Path) -> None:
    runner = HookActionRunner(
        project_root=tmp_path.resolve(),
        prompt_sink=RecordingPromptSink(),
    )
    success = runner.prepare(ShellHookAction("exit 0"), {})
    failure = runner.prepare(ShellHookAction("exit 7"), {})

    await runner.execute(success, event_sequence=1, rule_id="success")
    with pytest.raises(HookActionError) as captured:
        await runner.execute(failure, event_sequence=2, rule_id="failure")
    await runner.close()

    assert captured.value.code == "hook_shell_failed"


async def test_cancelled_shell_is_terminated_and_reaped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminated = False
            self._finished = asyncio.Event()

        async def wait(self) -> int:
            await self._finished.wait()
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0
            self._finished.set()

        def kill(self) -> None:
            self.returncode = -9
            self._finished.set()

    process = FakeProcess()

    async def create_process(*_args: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    runner = HookActionRunner(
        project_root=tmp_path.resolve(),
        prompt_sink=RecordingPromptSink(),
    )
    action = runner.prepare(ShellHookAction("long running"), {})
    task = asyncio.create_task(
        runner.execute(action, event_sequence=1, rule_id="cancel")
    )
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await runner.close()

    assert process.terminated is True
