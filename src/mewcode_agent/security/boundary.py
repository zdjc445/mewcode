"""Non-overridable command and filesystem safety boundaries."""

from __future__ import annotations

from typing import Any

from mewcode_agent.security.command_guard import DangerousCommandGuard
from mewcode_agent.security.models import PolicyDecision, SecurityRequest
from mewcode_agent.security.path_sandbox import PathSandbox, PathSandboxError


_PATH_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "write_file": ("path",),
    "edit_file": ("path",),
    "find_files": ("path",),
    "search_code": ("path",),
    "run_command": ("cwd",),
}
_DEFAULT_PATH_ARGUMENTS = {
    ("find_files", "path"),
    ("search_code", "path"),
    ("run_command", "cwd"),
}
_PATTERN_ARGUMENTS = {
    ("find_files", "pattern"),
    ("search_code", "file_pattern"),
}


class SecurityBoundary:
    def __init__(
        self,
        path_sandbox: PathSandbox,
        command_guard: DangerousCommandGuard | None = None,
    ) -> None:
        self._path_sandbox = path_sandbox
        self._command_guard = command_guard or DangerousCommandGuard()

    @property
    def path_sandbox(self) -> PathSandbox:
        return self._path_sandbox

    def evaluate(self, request: SecurityRequest) -> PolicyDecision | None:
        if request.tool_name == "run_command":
            command = request.arguments.get("command")
            reason = (
                self._command_guard.classify(command)
                if isinstance(command, str)
                else None
            )
            if reason is not None:
                return PolicyDecision("deny", reason)

        for argument in _PATH_ARGUMENTS.get(request.tool_name, ()):
            value = request.arguments.get(argument)
            if (
                value is None
                and (request.tool_name, argument) in _DEFAULT_PATH_ARGUMENTS
            ):
                value = str(self._path_sandbox.working_directory)
            if value is None or not isinstance(value, str):
                continue
            try:
                self._path_sandbox.resolve(value)
            except PathSandboxError:
                return PolicyDecision("deny", "path_outside_sandbox")

        for tool_name, argument in _PATTERN_ARGUMENTS:
            if request.tool_name != tool_name:
                continue
            value: Any = request.arguments.get(argument)
            if value is None and argument == "file_pattern":
                value = "**/*"
            if isinstance(value, str) and not PathSandbox.pattern_is_safe(value):
                return PolicyDecision("deny", "path_pattern_escape")
        return None

    def normalized_path_argument(
        self,
        request: SecurityRequest,
        argument: str,
    ) -> str | None:
        value = request.arguments.get(argument)
        if value is None and (request.tool_name, argument) in _DEFAULT_PATH_ARGUMENTS:
            value = str(self._path_sandbox.working_directory)
        if not isinstance(value, str):
            return None
        try:
            return self._path_sandbox.relative_posix(value)
        except PathSandboxError:
            return None

    @staticmethod
    def is_path_argument(tool_name: str, argument: str) -> bool:
        return argument in _PATH_ARGUMENTS.get(tool_name, ())
