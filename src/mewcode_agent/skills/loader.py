"""Strict YAML-frontmatter and directory-tool loading for Skills."""

from __future__ import annotations

from collections.abc import Mapping
import math
from pathlib import Path, PurePosixPath
from typing import Any, cast

from jsonschema import Draft202012Validator, SchemaError
import yaml

from mewcode_agent.skills.models import (
    SKILL_NAME_PATTERN,
    SKILL_TOOL_NAME_PATTERN,
    SkillConfigError,
    SkillDefinition,
    SkillSource,
    SkillToolDefinition,
)


_FRONTMATTER_KEYS = {
    "name",
    "description",
    "allowed_tools",
    "execution_mode",
    "model",
    "context_strategy",
    "recent_messages",
}
_MANIFEST_KEYS = {"version", "tools"}
_TOOL_KEYS = {
    "name",
    "description",
    "parameters",
    "category",
    "timeout_seconds",
    "script",
}


class _StrictSkillLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _StrictSkillLoader,
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
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSkillLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _error(code: str, message: str, *, cause: Exception | None = None) -> SkillConfigError:
    error = SkillConfigError(code, message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _read_utf8(path: Path, *, label: str, code: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _error(code, f"无法读取{label}", cause=exc)


def _yaml_mapping(text: str, *, label: str, code: str) -> Mapping[str, Any]:
    try:
        raw = yaml.load(text, Loader=_StrictSkillLoader)
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
    extra = sorted(actual - expected)
    if missing:
        raise _error(code, f"{label}缺少字段: {', '.join(missing)}")
    if extra:
        raise _error(code, f"{label}包含未知字段: {', '.join(extra)}")


def _split_document(text: str) -> tuple[str, str]:
    if text.startswith("---\r\n"):
        offset = 5
    elif text.startswith("---\n"):
        offset = 4
    else:
        raise _error("skill_document_invalid", "Skill 文档缺少 YAML frontmatter 起始标记")
    position = offset
    closing_start: int | None = None
    closing_end: int | None = None
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
            closing_start = position
            closing_end = end
            break
        if newline < 0:
            break
        position = newline + 1
    if closing_start is None or closing_end is None:
        raise _error("skill_document_invalid", "Skill 文档缺少 YAML frontmatter 结束标记")
    frontmatter = text[offset:closing_start]
    body = text[closing_end:].strip()
    if not frontmatter.strip():
        raise _error("skill_document_invalid", "Skill frontmatter 不能为空")
    if not body:
        raise _error("skill_document_invalid", "Skill SOP 正文不能为空")
    return frontmatter, body


def _single_line(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(character in value for character in "\r\n\x00")
    ):
        raise _error("skill_metadata_invalid", f"{field} 必须是非空单行字符串")
    return value


def _allowed_tools(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _error("skill_metadata_invalid", "allowed_tools 必须是列表")
    tools: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise _error("skill_metadata_invalid", "allowed_tools 元素必须是非空字符串")
        tools.append(item)
    if len(tools) != len(set(tools)):
        raise _error("skill_metadata_invalid", "allowed_tools 不能包含重复名称")
    return tuple(tools)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _json_value(value: Any, *, label: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise _error("skill_manifest_invalid", f"{label} 包含非有限浮点数")
    if isinstance(value, list):
        return [
            _json_value(item, label=f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _error(
                    "skill_manifest_invalid",
                    f"{label} 包含非字符串 JSON object key",
                )
            normalized[key] = _json_value(item, label=f"{label}.{key}")
        return normalized
    raise _error("skill_manifest_invalid", f"{label} 包含非 JSON 值")


def _resolved_file(path: Path, root: Path, *, label: str) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _error("skill_path_invalid", f"{label}不存在或无法解析", cause=exc)
    if not _within(resolved, resolved_root) or not resolved.is_file():
        raise _error("skill_path_invalid", f"{label}超出 Skill 目录或不是普通文件")
    return resolved


def _load_manifest(skill_directory: Path, source_root: Path) -> tuple[SkillToolDefinition, ...]:
    manifest_path = skill_directory / "tools.yaml"
    if not manifest_path.exists():
        return ()
    resolved_manifest = _resolved_file(manifest_path, source_root, label="tools.yaml")
    data = _yaml_mapping(
        _read_utf8(resolved_manifest, label="tools.yaml", code="skill_manifest_invalid"),
        label="tools.yaml",
        code="skill_manifest_invalid",
    )
    _require_exact_keys(data, _MANIFEST_KEYS, label="tools.yaml", code="skill_manifest_invalid")
    if type(data["version"]) is not int or data["version"] != 1:
        raise _error("skill_manifest_invalid", "tools.yaml version 必须是整数 1")
    raw_tools = data["tools"]
    if not isinstance(raw_tools, list):
        raise _error("skill_manifest_invalid", "tools.yaml tools 必须是列表")
    definitions: list[SkillToolDefinition] = []
    names: set[str] = set()
    resolved_directory = skill_directory.resolve(strict=True)
    for index, raw_tool in enumerate(raw_tools):
        label = f"tools.yaml tools[{index}]"
        if not isinstance(raw_tool, Mapping) or any(not isinstance(key, str) for key in raw_tool):
            raise _error("skill_manifest_invalid", f"{label} 必须是字符串键映射")
        tool = cast(Mapping[str, Any], raw_tool)
        _require_exact_keys(tool, _TOOL_KEYS, label=label, code="skill_manifest_invalid")
        name = tool["name"]
        if (
            not isinstance(name, str)
            or SKILL_TOOL_NAME_PATTERN.fullmatch(name) is None
        ):
            raise _error(
                "skill_manifest_invalid",
                f"{label}.name 必须匹配 [a-z][a-z0-9_]{{0,63}}",
            )
        if name in names:
            raise _error("skill_manifest_invalid", f"{label}.name 重复")
        names.add(name)
        description = tool["description"]
        if (
            not isinstance(description, str)
            or not description.strip()
            or any(character in description for character in "\r\n\x00")
        ):
            raise _error("skill_manifest_invalid", f"{label}.description 必须是非空单行字符串")
        parameters = tool["parameters"]
        if not isinstance(parameters, Mapping) or any(not isinstance(key, str) for key in parameters):
            raise _error("skill_manifest_invalid", f"{label}.parameters 必须是字符串键映射")
        schema = cast(
            dict[str, Any],
            _json_value(parameters, label=f"{label}.parameters"),
        )
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise _error("skill_manifest_invalid", f"{label}.parameters 不是有效 JSON Schema", cause=exc)
        if tool["category"] != "command":
            raise _error("skill_manifest_invalid", f"{label}.category 必须为 command")
        timeout = tool["timeout_seconds"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
            or float(timeout) > 300
        ):
            raise _error("skill_manifest_invalid", f"{label}.timeout_seconds 必须大于 0 且不超过 300")
        script = tool["script"]
        if not isinstance(script, str) or not script or "\\" in script:
            raise _error("skill_manifest_invalid", f"{label}.script 必须是相对 POSIX Python 路径")
        pure_script = PurePosixPath(script)
        if (
            pure_script.is_absolute()
            or pure_script.suffix != ".py"
            or any(part in ("", ".", "..") for part in pure_script.parts)
        ):
            raise _error("skill_manifest_invalid", f"{label}.script 必须是相对 POSIX Python 路径")
        script_path = _resolved_file(
            resolved_directory.joinpath(*pure_script.parts),
            resolved_directory,
            label=f"{label}.script",
        )
        definitions.append(
            SkillToolDefinition(
                name,
                description,
                schema,
                "command",
                float(timeout),
                script,
                script_path,
            )
        )
    return tuple(definitions)


def load_skill_definition(
    path: Path,
    *,
    source: SkillSource,
    source_root: Path,
    directory_skill: bool,
) -> SkillDefinition:
    """Load one exact Skill candidate without applying layer precedence."""

    if source not in ("builtin", "user", "project"):
        raise ValueError("source 无效")
    resolved_path = _resolved_file(path, source_root, label="Skill 文档")
    text = _read_utf8(resolved_path, label="Skill 文档", code="skill_document_invalid")
    frontmatter, body = _split_document(text)
    data = _yaml_mapping(frontmatter, label="Skill frontmatter", code="skill_document_invalid")
    _require_exact_keys(
        data,
        _FRONTMATTER_KEYS,
        label="Skill frontmatter",
        code="skill_metadata_invalid",
    )
    name = data["name"]
    if not isinstance(name, str) or SKILL_NAME_PATTERN.fullmatch(name) is None:
        raise _error("skill_metadata_invalid", "name 格式无效")
    description = _single_line(data["description"], field="description")
    allowed_tools = _allowed_tools(data["allowed_tools"])
    execution_mode = data["execution_mode"]
    if execution_mode not in ("shared", "isolated"):
        raise _error("skill_metadata_invalid", "execution_mode 必须为 shared 或 isolated")
    if data["model"] != "inherit":
        raise _error("skill_metadata_invalid", "model 必须为 inherit")
    context_strategy = data["context_strategy"]
    if context_strategy not in ("current", "summary", "recent", "none"):
        raise _error("skill_metadata_invalid", "context_strategy 无效")
    recent_messages = data["recent_messages"]
    if execution_mode == "shared":
        if context_strategy != "current" or recent_messages is not None:
            raise _error("skill_metadata_invalid", "shared 必须使用 current 和 null recent_messages")
    elif context_strategy == "recent":
        if type(recent_messages) is not int or recent_messages <= 0:
            raise _error("skill_metadata_invalid", "recent 必须设置正整数 recent_messages")
    elif recent_messages is not None:
        raise _error("skill_metadata_invalid", "summary 或 none 必须使用 null recent_messages")
    skill_directory = resolved_path.parent if directory_skill else None
    dedicated_tools = (
        _load_manifest(skill_directory, source_root)
        if skill_directory is not None
        else ()
    )
    return SkillDefinition(
        name=name,
        description=description,
        allowed_tools=allowed_tools,
        execution_mode=cast(Any, execution_mode),
        model="inherit",
        context_strategy=cast(Any, context_strategy),
        recent_messages=cast(int | None, recent_messages),
        body=body,
        source=source,
        source_root=source_root.resolve(strict=True),
        source_path=resolved_path,
        skill_directory=skill_directory,
        dedicated_tools=dedicated_tools,
    )
