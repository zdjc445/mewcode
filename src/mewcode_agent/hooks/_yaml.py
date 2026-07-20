"""Strict YAML helpers for Hook configuration files."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from mewcode_agent.hooks.models import HookConfigError


class StrictHookLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: StrictHookLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictHookLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def read_hook_yaml(
    path: Path,
    *,
    label: str,
) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise HookConfigError(f"{label} Hook 配置路径不是文件")
    try:
        raw = yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=StrictHookLoader,
        )
    except (OSError, UnicodeError) as exc:
        raise HookConfigError(f"无法读取{label} Hook 配置") from exc
    except yaml.YAMLError as exc:
        raise HookConfigError(f"{label} Hook 配置不是有效 YAML") from exc
    if not isinstance(raw, Mapping):
        raise HookConfigError(f"{label} Hook 配置根节点必须是映射")
    if any(not isinstance(key, str) for key in raw):
        raise HookConfigError(f"{label} Hook 配置字段名必须是字符串")
    return raw


def require_exact_keys(
    data: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
    location: str,
) -> None:
    optional = optional or set()
    actual = set(data)
    missing = sorted(required - actual)
    unknown = sorted(actual - required - optional)
    if missing:
        raise HookConfigError(f"{location} 缺少字段: {', '.join(missing)}")
    if unknown:
        raise HookConfigError(f"{location} 包含未知字段: {', '.join(unknown)}")
