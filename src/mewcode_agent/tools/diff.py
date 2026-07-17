"""Bounded unified diffs for file-edit results."""

from __future__ import annotations

from dataclasses import dataclass
import difflib


MAX_DIFF_LINES = 200


@dataclass(frozen=True, slots=True)
class DiffResult:
    text: str
    additions: int
    removals: int
    truncated: bool


def build_diff(old_content: str, new_content: str) -> DiffResult:
    """Return a line-based unified diff with bounded display output."""

    raw_lines = list(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile="before",
            tofile="after",
            lineterm="\n",
            n=3,
        )
    )
    additions = sum(1 for line in raw_lines[2:] if line.startswith("+"))
    removals = sum(1 for line in raw_lines[2:] if line.startswith("-"))

    rendered_lines: list[str] = []
    for line in raw_lines:
        if line.endswith("\n"):
            rendered_lines.append(line[:-1])
            continue
        rendered_lines.append(line)
        if line.startswith((" ", "+", "-")):
            rendered_lines.append("\\ No newline at end of file")

    truncated = len(rendered_lines) > MAX_DIFF_LINES
    visible_lines = rendered_lines[:MAX_DIFF_LINES]
    if truncated:
        visible_lines.append(
            f"... diff 已截断，只显示前 {MAX_DIFF_LINES} 行"
        )

    return DiffResult(
        text="\n".join(visible_lines),
        additions=additions,
        removals=removals,
        truncated=truncated,
    )
