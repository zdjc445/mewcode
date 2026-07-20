"""Deterministic slash-command line parsing."""

from __future__ import annotations

import re

from mewcode_agent.commands.models import ParsedCommandLine


_INVOCATION_NAME = re.compile(r"(?:[A-Za-z][A-Za-z0-9-]*|\?)\Z")


def parse_command_line(value: str) -> ParsedCommandLine:
    if not isinstance(value, str):
        raise TypeError("value 必须是字符串")
    normalized = value.strip()
    if not normalized or not normalized.startswith("/"):
        return ParsedCommandLine(False, False, None, "")

    body = normalized[1:]
    if " " in body:
        raw_name, arguments = body.split(" ", 1)
        arguments = arguments.strip(" ")
    else:
        raw_name = body
        arguments = ""
    if _INVOCATION_NAME.fullmatch(raw_name) is None:
        return ParsedCommandLine(True, False, None, arguments)
    return ParsedCommandLine(True, True, raw_name.lower(), arguments)

