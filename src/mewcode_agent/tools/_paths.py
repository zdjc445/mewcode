"""Shared path conversion for built-in tools."""

from pathlib import Path


def expand_path(value: str) -> Path:
    return Path(value).expanduser()

