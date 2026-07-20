"""Strict user-level worktree runtime configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from mewcode_agent.worktrees.models import (
    WorktreeConfigError,
    WorktreeRuntimeConfig,
    validate_relative_config_path,
)


_KEYS = {
    "version",
    "stale_after_hours",
    "cleanup_interval_seconds",
    "local_config_files",
    "dependency_links",
    "copy_ignored",
}


class _StrictLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _StrictLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _error(message: str, *, cause: Exception | None = None) -> WorktreeConfigError:
    error = WorktreeConfigError("worktree_config_invalid", message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _path_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _error(f"{field_name} 必须是列表")
    result: list[str] = []
    for item in value:
        try:
            result.append(validate_relative_config_path(item))
        except ValueError as exc:
            raise _error(f"{field_name} 包含无效路径", cause=exc)
    if len(result) != len(set(result)):
        raise _error(f"{field_name} 不能包含重复路径")
    return tuple(result)


def load_worktree_config(path: Path) -> WorktreeRuntimeConfig:
    if not path.exists():
        return WorktreeRuntimeConfig()
    if not path.is_file():
        raise _error("Worktree 配置路径不是文件")
    try:
        text = path.read_text(encoding="utf-8")
        raw = yaml.load(text, Loader=_StrictLoader)
    except (OSError, UnicodeError) as exc:
        raise _error("无法读取 Worktree 配置", cause=exc)
    except yaml.YAMLError as exc:
        raise _error("Worktree 配置不是有效 YAML", cause=exc)
    if not isinstance(raw, Mapping) or any(
        not isinstance(key, str) for key in raw
    ):
        raise _error("Worktree 配置根节点必须是字符串映射")
    data = cast(Mapping[str, Any], raw)
    missing = sorted(_KEYS - set(data))
    unknown = sorted(set(data) - _KEYS)
    if missing:
        raise _error(f"Worktree 配置缺少字段: {', '.join(missing)}")
    if unknown:
        raise _error(f"Worktree 配置包含未知字段: {', '.join(unknown)}")
    if type(data["version"]) is not int or data["version"] != 1:
        raise _error("Worktree 配置 version 必须是整数 1")
    try:
        return WorktreeRuntimeConfig(
            data["stale_after_hours"],
            data["cleanup_interval_seconds"],
            _path_list(data["local_config_files"], "local_config_files"),
            _path_list(data["dependency_links"], "dependency_links"),
            _path_list(data["copy_ignored"], "copy_ignored"),
        )
    except ValueError as exc:
        raise _error(f"Worktree 配置无效: {exc}", cause=exc)
