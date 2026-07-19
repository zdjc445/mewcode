"""Strict two-layer loading for external prompt modules."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any, cast

import yaml

from mewcode_agent.prompting.builtins import BUILTIN_MODULES
from mewcode_agent.prompting.models import (
    PromptModule,
    PromptModuleSource,
    validate_prompt_identifier,
)

_ROOT_KEYS = {"version", "modules"}
_ENABLED_KEYS = {"id", "enabled", "priority", "content"}
_DISABLED_KEYS = {"id", "enabled"}
_YAML_BOOL_TAG = "tag:yaml.org,2002:bool"


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that accepts only exact true/false booleans."""


_StrictSafeLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern)
        for tag, pattern in resolvers
        if tag != _YAML_BOOL_TAG
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictSafeLoader.add_implicit_resolver(
    _YAML_BOOL_TAG,
    re.compile(r"^(?:true|false)$"),
    ["t", "f"],
)


class PromptConfigError(RuntimeError):
    """A safe startup error for prompt configuration."""


def _prefix(layer: str, path: Path, field: str | None = None) -> str:
    base = f"{layer} Prompt 配置 {path}"
    return f"{base} 的 {field}" if field else base


def _expect_mapping(
    value: Any,
    *,
    layer: str,
    path: Path,
    field: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PromptConfigError(
            f"{_prefix(layer, path, field)} 必须是映射"
        )
    return cast(Mapping[str, Any], value)


def _validate_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    layer: str,
    path: Path,
    field: str,
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(str(item) for item in actual - expected)
    if missing:
        raise PromptConfigError(
            f"{_prefix(layer, path, field)} 缺少字段: "
            f"{', '.join(missing)}"
        )
    if extra:
        raise PromptConfigError(
            f"{_prefix(layer, path, field)} 包含未知字段: "
            f"{', '.join(extra)}"
        )


def _read_layer(path: Path, layer: str) -> list[Mapping[str, Any]]:
    if not path.exists():
        return []
    if not path.is_file():
        raise PromptConfigError(f"{_prefix(layer, path)} 不是文件")
    try:
        raw = yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=_StrictSafeLoader,
        )
    except (OSError, UnicodeError) as exc:
        raise PromptConfigError(f"无法读取 {_prefix(layer, path)}") from exc
    except yaml.YAMLError as exc:
        raise PromptConfigError(
            f"{_prefix(layer, path)} 不是有效 YAML"
        ) from exc

    root = _expect_mapping(raw, layer=layer, path=path, field="根节点")
    _validate_exact_keys(
        root,
        _ROOT_KEYS,
        layer=layer,
        path=path,
        field="根节点",
    )
    if type(root["version"]) is not int or root["version"] != 1:
        raise PromptConfigError(
            f"{_prefix(layer, path, 'version')} 必须为整数 1"
        )
    modules = root["modules"]
    if not isinstance(modules, list):
        raise PromptConfigError(
            f"{_prefix(layer, path, 'modules')} 必须是列表"
        )

    parsed: list[Mapping[str, Any]] = []
    for index, item in enumerate(modules):
        parsed.append(
            _expect_mapping(
                item,
                layer=layer,
                path=path,
                field=f"modules[{index}]",
            )
        )
    return parsed


def _apply_layer(
    catalog: dict[str, PromptModule],
    *,
    entries: list[Mapping[str, Any]],
    layer: str,
    source: PromptModuleSource,
    path: Path,
) -> None:
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        field = f"modules[{index}]"
        if "enabled" not in entry or type(entry["enabled"]) is not bool:
            raise PromptConfigError(
                f"{_prefix(layer, path, field + '.enabled')} 必须为布尔值"
            )
        expected = _ENABLED_KEYS if entry["enabled"] else _DISABLED_KEYS
        _validate_exact_keys(
            entry,
            expected,
            layer=layer,
            path=path,
            field=field,
        )
        module_id = entry["id"]
        try:
            validate_prompt_identifier(module_id, f"{field}.id")
        except ValueError as exc:
            raise PromptConfigError(
                f"{_prefix(layer, path, field + '.id')} 不符合精确格式"
            ) from exc
        module_id = cast(str, module_id)
        if module_id in seen:
            raise PromptConfigError(
                f"{_prefix(layer, path, field + '.id')} 出现重复 id: "
                f"{module_id}"
            )
        seen.add(module_id)
        if module_id == "core" or module_id.startswith("core."):
            raise PromptConfigError(
                f"{_prefix(layer, path, field + '.id')} "
                "使用了保留 core 命名空间"
            )
        existing = catalog.get(module_id)
        if existing is not None and existing.protected:
            raise PromptConfigError(
                f"{_prefix(layer, path, field + '.id')} "
                "不能修改受保护模块"
            )
        if not entry["enabled"]:
            if existing is None:
                raise PromptConfigError(
                    f"{_prefix(layer, path, field + '.id')} "
                    "要禁用的模块不存在"
                )
            del catalog[module_id]
            continue

        priority = entry["priority"]
        if type(priority) is not int:
            raise PromptConfigError(
                f"{_prefix(layer, path, field + '.priority')} 必须为整数"
            )
        content = entry["content"]
        if not isinstance(content, str) or not content.strip():
            raise PromptConfigError(
                f"{_prefix(layer, path, field + '.content')} "
                "必须为非空字符串"
            )
        catalog[module_id] = PromptModule(
            module_id=module_id,
            priority=priority,
            content=content,
            source=source,
            protected=False,
        )


def load_prompt_modules(
    *,
    user_path: Path,
    project_path: Path,
) -> tuple[PromptModule, ...]:
    catalog = {item.module_id: item for item in BUILTIN_MODULES}
    for layer, source, path in (
        ("用户全局", "user", user_path),
        ("项目", "project", project_path),
    ):
        _apply_layer(
            catalog,
            entries=_read_layer(path, layer),
            layer=layer,
            source=cast(PromptModuleSource, source),
            path=path,
        )
    return tuple(
        sorted(
            catalog.values(),
            key=lambda item: (item.priority, item.module_id),
        )
    )
