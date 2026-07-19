"""Strict YAML parsing shared by security configuration files."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


class SecurityConfigError(RuntimeError):
    """A safe, user-facing security configuration error."""


class StrictSecurityLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: StrictSecurityLoader,
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
                f"found duplicate key ({key})",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictSecurityLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def read_yaml_mapping(path: Path, *, label: str) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise SecurityConfigError(f"{label}安全配置路径不是文件: {path}")
    try:
        raw = yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=StrictSecurityLoader,
        )
    except (OSError, UnicodeError) as exc:
        raise SecurityConfigError(f"无法读取{label}安全配置: {path}") from exc
    except yaml.YAMLError as exc:
        raise SecurityConfigError(f"{label}安全配置不是有效 YAML: {path}") from exc
    if not isinstance(raw, Mapping):
        raise SecurityConfigError(f"{label}安全配置根节点必须是映射: {path}")
    if any(not isinstance(key, str) for key in raw):
        raise SecurityConfigError(f"{label}安全配置字段名必须是字符串: {path}")
    return raw


def require_exact_keys(
    data: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
    location: str,
) -> None:
    actual = set(data)
    missing = sorted(required - actual)
    unknown = sorted(actual - required - optional)
    if missing:
        raise SecurityConfigError(
            f"{location} 缺少字段: {', '.join(missing)}"
        )
    if unknown:
        raise SecurityConfigError(
            f"{location} 包含未知字段: {', '.join(unknown)}"
        )
