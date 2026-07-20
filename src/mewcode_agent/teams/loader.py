"""Strict user-level Team runtime configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from mewcode_agent.teams.models import TeamConfigError, TeamRuntimeConfig


_KEYS = {
    "version",
    "max_teams",
    "max_members_per_team",
    "max_tasks_per_team",
    "scheduler_interval_seconds",
    "member_timeout_seconds",
    "member_history_messages",
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


def _error(message: str, cause: Exception | None = None) -> TeamConfigError:
    error = TeamConfigError("team_config_invalid", message)
    if cause is not None:
        error.__cause__ = cause
    return error


def load_team_config(path: Path) -> TeamRuntimeConfig:
    if not path.exists():
        return TeamRuntimeConfig()
    if not path.is_file():
        raise _error("Team 配置路径不是文件")
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except (OSError, UnicodeError) as exc:
        raise _error("无法读取 Team 配置", exc)
    except yaml.YAMLError as exc:
        raise _error("Team 配置不是有效 YAML", exc)
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise _error("Team 配置根节点必须是字符串映射")
    data = cast(Mapping[str, Any], raw)
    if set(data) != _KEYS:
        raise _error("Team 配置字段不完整或包含未知字段")
    if type(data["version"]) is not int or data["version"] != 1:
        raise _error("Team 配置 version 必须是整数 1")
    try:
        return TeamRuntimeConfig(
            max_teams=data["max_teams"],
            max_members_per_team=data["max_members_per_team"],
            max_tasks_per_team=data["max_tasks_per_team"],
            scheduler_interval_seconds=data["scheduler_interval_seconds"],
            member_timeout_seconds=data["member_timeout_seconds"],
            member_history_messages=data["member_history_messages"],
        )
    except (TypeError, ValueError) as exc:
        raise _error("Team 配置内容无效", exc)
