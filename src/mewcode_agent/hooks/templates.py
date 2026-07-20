"""Strict `${context.path}` expansion for Hook actions."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any

from mewcode_agent.hooks.models import validate_context_path


_PLACEHOLDER = re.compile(r"\$\{([^{}]*)\}")


class HookTemplateError(RuntimeError):
    pass


def validate_template(template: str) -> None:
    if not isinstance(template, str):
        raise ValueError("模板必须是字符串")
    consumed: list[tuple[int, int]] = []
    for match in _PLACEHOLDER.finditer(template):
        path = match.group(1)
        if not validate_context_path(path):
            raise ValueError("模板包含无效上下文字段")
        consumed.append(match.span())
    remainder_parts: list[str] = []
    position = 0
    for start, end in consumed:
        remainder_parts.append(template[position:start])
        position = end
    remainder_parts.append(template[position:])
    if "${" in "".join(remainder_parts):
        raise ValueError("模板包含未闭合或嵌套占位符")


def render_template(template: str, context: Mapping[str, Any]) -> str:
    validate_template(template)

    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        if path not in context:
            return ""
        value = context[path]
        if isinstance(value, str):
            return value
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise HookTemplateError("上下文字段不能安全序列化") from exc

    return _PLACEHOLDER.sub(replace, template)
