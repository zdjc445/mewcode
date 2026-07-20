"""Side-effect executors for prepared Hook actions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Protocol, TypeAlias
from urllib.parse import urlsplit

import httpx

from mewcode_agent.hooks.models import (
    HookAction,
    HttpHookAction,
    PromptHookAction,
    ShellHookAction,
    SubagentHookAction,
)
from mewcode_agent.hooks.templates import render_template


class HookPromptSink(Protocol):
    async def inject(
        self,
        content: str,
        *,
        event_sequence: int,
        rule_id: str,
    ) -> None: ...

    async def flush(self) -> tuple[str, ...]: ...

    def reset_session(
        self,
        *,
        preserve_rule_ids: frozenset[str],
    ) -> int: ...

    def discard_pending(self) -> int: ...


class HookActionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class PreparedShellAction:
    command: str


@dataclass(frozen=True, slots=True)
class PreparedPromptAction:
    content: str


@dataclass(frozen=True, slots=True)
class PreparedHttpAction:
    method: str
    url: str
    headers: dict[str, str]
    body: str


@dataclass(frozen=True, slots=True)
class PreparedSubagentAction:
    task: str
    context: str


PreparedHookAction: TypeAlias = (
    PreparedShellAction
    | PreparedPromptAction
    | PreparedHttpAction
    | PreparedSubagentAction
)
HookSubagentRunner: TypeAlias = Callable[[str, str], Awaitable[None]]


class HookActionRunner:
    def __init__(
        self,
        *,
        project_root: Path,
        prompt_sink: HookPromptSink,
        http_client: httpx.AsyncClient | None = None,
        subagent_runner: HookSubagentRunner | None = None,
    ) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise ValueError("project_root 必须是绝对 Path")
        self._project_root = project_root
        self._project_root_binding: ContextVar[Path | None] = ContextVar(
            f"mewcode_hook_action_root_{id(self)}",
            default=None,
        )
        self._prompt_sink = prompt_sink
        self._http_client = http_client or httpx.AsyncClient(
            follow_redirects=False
        )
        self._subagent_runner = subagent_runner
        self._closed = False

    @property
    def project_root(self) -> Path:
        return self._project_root_binding.get() or self._project_root

    @contextmanager
    def bind_project_root(self, path: Path) -> Iterator[Path]:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("project_root binding 必须是绝对 Path")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("无法解析 project_root binding") from exc
        if not resolved.is_dir():
            raise ValueError("project_root binding 不是目录")
        token = self._project_root_binding.set(resolved)
        try:
            yield resolved
        finally:
            self._project_root_binding.reset(token)

    def set_subagent_runner(self, runner: HookSubagentRunner) -> None:
        if not callable(runner):
            raise ValueError("subagent runner 必须可调用")
        self._subagent_runner = runner

    def prepare(
        self,
        action: HookAction,
        context: dict[str, object],
    ) -> PreparedHookAction:
        if isinstance(action, ShellHookAction):
            return PreparedShellAction(
                render_template(action.command, context)
            )
        if isinstance(action, PromptHookAction):
            return PreparedPromptAction(
                render_template(action.content, context)
            )
        if isinstance(action, HttpHookAction):
            url = render_template(action.url, context)
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise HookActionError(
                    "hook_http_failed",
                    "Hook HTTP URL 无效",
                )
            headers = {
                name: render_template(value, context)
                for name, value in action.headers.items()
            }
            if any(
                "\r" in value or "\n" in value
                for value in headers.values()
            ):
                raise HookActionError(
                    "hook_http_failed",
                    "Hook HTTP header 无效",
                )
            return PreparedHttpAction(
                action.method,
                url,
                headers,
                render_template(action.body, context),
            )
        assert isinstance(action, SubagentHookAction)
        return PreparedSubagentAction(
            render_template(action.task, context),
            action.context,
        )

    async def execute(
        self,
        action: PreparedHookAction,
        *,
        event_sequence: int,
        rule_id: str,
    ) -> None:
        if self._closed:
            raise HookActionError(
                "hook_action_failed",
                "Hook 动作执行器已关闭",
            )
        if isinstance(action, PreparedShellAction):
            await self._run_shell(action.command)
            return
        if isinstance(action, PreparedPromptAction):
            try:
                await self._prompt_sink.inject(
                    action.content,
                    event_sequence=event_sequence,
                    rule_id=rule_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise HookActionError(
                    "hook_prompt_failed",
                    "Hook Prompt 注入失败",
                ) from exc
            return
        if isinstance(action, PreparedHttpAction):
            try:
                response = await self._http_client.request(
                    action.method,
                    action.url,
                    headers=action.headers,
                    content=action.body,
                    follow_redirects=False,
                )
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as exc:
                raise HookActionError(
                    "hook_http_failed",
                    "Hook HTTP 请求失败",
                ) from exc
            if response.status_code < 200 or response.status_code >= 300:
                raise HookActionError(
                    "hook_http_failed",
                    "Hook HTTP 返回非成功状态",
                )
            return
        assert isinstance(action, PreparedSubagentAction)
        if self._subagent_runner is None:
            raise HookActionError(
                "hook_subagent_unavailable",
                "Hook subagent 执行器尚未接入",
            )
        try:
            await self._subagent_runner(action.task, action.context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise HookActionError(
                "hook_subagent_failed",
                "Hook subagent 启动失败",
            ) from exc

    async def _run_shell(self, command: str) -> None:
        executable: str
        arguments: tuple[str, ...]
        if os.name == "nt":
            resolved = shutil.which("pwsh") or shutil.which("powershell")
            if resolved is None:
                raise HookActionError(
                    "hook_shell_failed",
                    "Hook shell 启动失败",
                )
            executable = resolved
            arguments = (
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            )
        else:
            executable = "/bin/sh"
            arguments = ("-c", command)
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                cwd=self.project_root,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            raise HookActionError(
                "hook_shell_failed",
                "Hook shell 启动失败",
            ) from exc
        try:
            return_code = await process.wait()
        except asyncio.CancelledError:
            await self._stop_process(process)
            raise
        if return_code != 0:
            raise HookActionError(
                "hook_shell_failed",
                "Hook shell 返回非零退出码",
            )

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            async with asyncio.timeout(2):
                await process.wait()
            return
        except TimeoutError:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()

    async def close(self) -> int:
        if self._closed:
            return 0
        self._closed = True
        pending = self._prompt_sink.discard_pending()
        await self._http_client.aclose()
        return pending

    async def flush_pending_prompts(self) -> tuple[str, ...]:
        return await self._prompt_sink.flush()

    def reset_prompt_session(
        self,
        *,
        preserve_rule_ids: frozenset[str],
    ) -> int:
        return self._prompt_sink.reset_session(
            preserve_rule_ids=preserve_rule_ids
        )
