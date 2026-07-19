"""Non-overridable detection of known destructive shell commands."""

from __future__ import annotations

import re


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "recursive_force_delete",
        re.compile(
            r"(?:^|[;&|]\s*)(?:sudo\s+)?rm\b"
            r"(?=[^;&|\r\n]*\s-[^\s;&|]*r)"
            r"(?=[^;&|\r\n]*\s-[^\s;&|]*f)",
            re.IGNORECASE,
        ),
    ),
    (
        "recursive_force_delete",
        re.compile(
            r"\bRemove-Item\b"
            r"(?=[^;\r\n]*\s-(?:Recurse|r)\b)"
            r"(?=[^;\r\n]*\s-(?:Force|fo)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "recursive_force_delete",
        re.compile(
            r"(?:^|[&|]\s*)(?:del|erase|rd|rmdir)\b"
            r"(?=[^&|\r\n]*\s/s\b)"
            r"(?=[^&|\r\n]*\s/q\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "disk_destructive_operation",
        re.compile(
            r"\b(?:Format-Volume|Clear-Disk|Initialize-Disk|"
            r"Remove-Partition|diskpart|mkfs(?:\.[a-z0-9]+)?|fdisk)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_power_operation",
        re.compile(
            r"(?:^|[;&|]\s*)(?:Stop-Computer|Restart-Computer|"
            r"shutdown|reboot|poweroff)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "remote_script_execution",
        re.compile(
            r"\b(?:curl(?:\.exe)?|wget(?:\.exe)?|"
            r"Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b"
            r"[\s\S]*?\|\s*(?:sudo\s+)?"
            r"(?:sh|bash|zsh|pwsh|powershell(?:\.exe)?|"
            r"cmd(?:\.exe)?|python(?:3)?|node|perl|ruby|"
            r"Invoke-Expression|iex)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "remote_script_execution",
        re.compile(
            r"\b(?:Invoke-Expression|iex)\s*(?:\(|\s)\s*"
            r"(?:Invoke-WebRequest|Invoke-RestMethod|iwr|irm|curl|wget)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "remote_script_execution",
        re.compile(
            r"\b(?:Invoke-Expression|iex)\b[\s\S]*?"
            r"\bDownloadString\s*\(",
            re.IGNORECASE,
        ),
    ),
    (
        "destructive_git_operation",
        re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    ),
    (
        "destructive_git_operation",
        re.compile(
            r"\bgit\s+clean\b(?=[^;&|\r\n]*\s-[^\s;&|]*f)"
            r"(?=[^;&|\r\n]*\s-[^\s;&|]*[dx])",
            re.IGNORECASE,
        ),
    ),
    (
        "shell_fork_bomb",
        re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    ),
)


class DangerousCommandGuard:
    def classify(self, command: str) -> str | None:
        if not isinstance(command, str):
            return None
        for reason_code, pattern in _PATTERNS:
            if pattern.search(command):
                return reason_code
        return None
