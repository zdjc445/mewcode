"""Strict YAML-frontmatter and runtime configuration loading for workers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from mewcode_agent.workers.models import (
    WORKER_NAME_PATTERN,
    WORKER_TOOL_NAME_PATTERN,
    WorkerConfigError,
    WorkerRoleDefinition,
    WorkerRuntimeConfig,
    WorkerSource,
)


_ROLE_KEYS = {
    "name",
    "description",
    "allowed_tools",
    "denied_tools",
    "model",
    "max_rounds",
    "permission_mode",
    "isolation",
}
_RUNTIME_KEYS = {
    "version",
    "max_concurrency",
    "foreground_timeout_seconds",
    "background_allowed_tools",
    "enable_verify_role",
}


class _StrictWorkerLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _StrictWorkerLoader,
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


_StrictWorkerLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _error(
    code: str,
    message: str,
    *,
    cause: Exception | None = None,
) -> WorkerConfigError:
    error = WorkerConfigError(code, message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _yaml_mapping(
    text: str,
    *,
    label: str,
    code: str,
) -> Mapping[str, Any]:
    try:
        raw = yaml.load(text, Loader=_StrictWorkerLoader)
    except yaml.YAMLError as exc:
        raise _error(code, f"{label}不是有效 YAML", cause=exc)
    if not isinstance(raw, Mapping):
        raise _error(code, f"{label}根节点必须是映射")
    if any(not isinstance(key, str) for key in raw):
        raise _error(code, f"{label}字段名必须是字符串")
    return cast(Mapping[str, Any], raw)


def _require_exact_keys(
    data: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
    code: str,
) -> None:
    actual = set(data)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise _error(code, f"{label}缺少字段: {', '.join(missing)}")
    if unknown:
        raise _error(code, f"{label}包含未知字段: {', '.join(unknown)}")


def _split_document(text: str) -> tuple[str, str]:
    if text.startswith("---\r\n"):
        offset = 5
    elif text.startswith("---\n"):
        offset = 4
    else:
        raise _error(
            "worker_document_invalid",
            "Worker 文档缺少 YAML frontmatter 起始标记",
        )
    position = offset
    while position <= len(text):
        newline = text.find("\n", position)
        if newline < 0:
            line = text[position:]
            end = len(text)
        else:
            line = text[position:newline]
            if line.endswith("\r"):
                line = line[:-1]
            end = newline + 1
        if line == "---":
            frontmatter = text[offset:position]
            body = text[end:].strip()
            if not frontmatter.strip():
                raise _error(
                    "worker_document_invalid",
                    "Worker frontmatter 不能为空",
                )
            if not body:
                raise _error(
                    "worker_document_invalid",
                    "Worker SOP 正文不能为空",
                )
            return frontmatter, body
        if newline < 0:
            break
        position = newline + 1
    raise _error(
        "worker_document_invalid",
        "Worker 文档缺少 YAML frontmatter 结束标记",
    )


def _read_utf8(path: Path, *, label: str, code: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _error(code, f"无法读取{label}", cause=exc)


def _single_line(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(char in value for char in "\r\n\x00")
    ):
        raise _error(
            "worker_metadata_invalid",
            f"{field} 必须是非空单行字符串",
        )
    return value


def _tool_list(
    value: Any,
    *,
    field: str,
    allow_null: bool,
) -> tuple[str, ...] | None:
    if value is None and allow_null:
        return None
    if not isinstance(value, list):
        expected = "列表或 null" if allow_null else "列表"
        raise _error(
            "worker_metadata_invalid",
            f"{field} 必须是{expected}",
        )
    result: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or WORKER_TOOL_NAME_PATTERN.fullmatch(item) is None
        ):
            raise _error(
                "worker_metadata_invalid",
                f"{field} 元素必须匹配 [a-z][a-z0-9_]{{0,63}}",
            )
        result.append(item)
    if len(result) != len(set(result)):
        raise _error(
            "worker_metadata_invalid",
            f"{field} 不能包含重复名称",
        )
    return tuple(result)


def load_worker_role(
    path: Path,
    *,
    source: WorkerSource,
    source_root: Path,
) -> WorkerRoleDefinition:
    try:
        resolved_root = source_root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise _error(
            "worker_document_invalid",
            "Worker 文档路径无效",
            cause=exc,
        )
    if not resolved_path.is_file():
        raise _error(
            "worker_document_invalid",
            "Worker 文档不是普通文件",
        )
    frontmatter, body = _split_document(
        _read_utf8(
            resolved_path,
            label="Worker 文档",
            code="worker_document_invalid",
        )
    )
    data = _yaml_mapping(
        frontmatter,
        label="Worker frontmatter",
        code="worker_document_invalid",
    )
    _require_exact_keys(
        data,
        _ROLE_KEYS,
        label="Worker frontmatter",
        code="worker_metadata_invalid",
    )
    name = data["name"]
    if (
        not isinstance(name, str)
        or WORKER_NAME_PATTERN.fullmatch(name) is None
    ):
        raise _error(
            "worker_metadata_invalid",
            "name 必须匹配 [a-z][a-z0-9-]{0,63}",
        )
    allowed = _tool_list(
        data["allowed_tools"],
        field="allowed_tools",
        allow_null=True,
    )
    denied = _tool_list(
        data["denied_tools"],
        field="denied_tools",
        allow_null=False,
    )
    assert denied is not None
    if allowed is not None and "spawn_worker" in allowed:
        raise _error(
            "worker_metadata_invalid",
            "allowed_tools 不能包含 spawn_worker",
        )
    if allowed is not None and set(allowed).intersection(denied):
        raise _error(
            "worker_metadata_invalid",
            "allowed_tools 与 denied_tools 不能重叠",
        )
    try:
        return WorkerRoleDefinition(
            name,
            _single_line(data["description"], field="description"),
            allowed,
            denied,
            _single_line(data["model"], field="model"),
            data["max_rounds"],
            data["permission_mode"],
            data["isolation"],
            body,
            source,
            resolved_root,
            resolved_path,
        )
    except ValueError as exc:
        raise _error(
            "worker_metadata_invalid",
            f"Worker frontmatter 无效: {exc}",
            cause=exc,
        )


def load_worker_runtime_config(path: Path) -> WorkerRuntimeConfig:
    if not path.exists():
        return WorkerRuntimeConfig()
    if not path.is_file():
        raise _error(
            "worker_config_invalid",
            "Worker 运行配置路径不是文件",
        )
    data = _yaml_mapping(
        _read_utf8(
            path,
            label="Worker 运行配置",
            code="worker_config_invalid",
        ),
        label="Worker 运行配置",
        code="worker_config_invalid",
    )
    _require_exact_keys(
        data,
        _RUNTIME_KEYS,
        label="Worker 运行配置",
        code="worker_config_invalid",
    )
    if type(data["version"]) is not int or data["version"] != 1:
        raise _error(
            "worker_config_invalid",
            "Worker 运行配置 version 必须是整数 1",
        )
    background = _tool_list(
        data["background_allowed_tools"],
        field="background_allowed_tools",
        allow_null=False,
    )
    assert background is not None
    try:
        return WorkerRuntimeConfig(
            data["max_concurrency"],
            data["foreground_timeout_seconds"],
            background,
            data["enable_verify_role"],
        )
    except ValueError as exc:
        raise _error(
            "worker_config_invalid",
            f"Worker 运行配置无效: {exc}",
            cause=exc,
        )
