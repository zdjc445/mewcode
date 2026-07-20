"""Tool adapters provided by the Skill runtime."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from mewcode_agent.skills.models import (
    SkillConfigError,
    SkillDefinition,
    SkillToolDefinition,
)
from mewcode_agent.tools.base import (
    Tool,
    ToolExecutionError,
    validate_arguments,
)

if TYPE_CHECKING:
    from mewcode_agent.skills.runtime import SkillRuntime


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


class SkillScriptTool(Tool):
    """Execute one directory-Skill Python tool through JSON stdin/stdout."""

    def __init__(
        self,
        definition: SkillToolDefinition,
        *,
        skill_directory: Path,
    ) -> None:
        if not isinstance(definition, SkillToolDefinition):
            raise ValueError("definition 类型无效")
        if not isinstance(skill_directory, Path) or not skill_directory.is_absolute():
            raise ValueError("skill_directory 必须是绝对 Path")
        self.name = definition.name
        self.description = definition.description
        self.parameters = definition.parameters
        self.category = definition.category
        self.timeout_seconds = definition.timeout_seconds
        self._script_path = definition.script_path
        self._skill_directory = skill_directory
        self._validator = Draft202012Validator(self.parameters)

    async def execute(self, arguments: dict[str, Any]) -> Any:
        errors = tuple(self._validator.iter_errors(arguments))
        if errors:
            raise ToolExecutionError(
                "invalid_arguments",
                "工具参数不符合 Skill 声明的 JSON Schema",
            )
        try:
            encoded = (
                json.dumps(
                    arguments,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ToolExecutionError(
                "invalid_arguments",
                "工具参数无法编码为 JSON",
            ) from exc
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(self._script_path),
                cwd=str(self._skill_directory),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ToolExecutionError(
                "skill_script_failed",
                "Skill 工具脚本无法启动",
            ) from exc
        try:
            stdout, _stderr = await process.communicate(encoded)
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            raise ToolExecutionError(
                "skill_script_failed",
                "Skill 工具脚本执行失败",
            )
        try:
            text = stdout.decode("utf-8")
            return json.loads(text, parse_constant=_reject_json_constant)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ToolExecutionError(
                "skill_script_output_invalid",
                "Skill 工具脚本未返回有效 JSON",
            ) from exc


def build_skill_script_tools(
    definitions: tuple[SkillDefinition, ...],
) -> tuple[SkillScriptTool, ...]:
    tools: list[SkillScriptTool] = []
    for definition in definitions:
        if definition.skill_directory is None:
            if definition.dedicated_tools:
                raise ValueError("单文件 Skill 不能包含专属工具")
            continue
        tools.extend(
            SkillScriptTool(
                tool,
                skill_directory=definition.skill_directory,
            )
            for tool in definition.dedicated_tools
        )
    return tuple(tools)


class LoadSkillTool(Tool):
    """System-level control tool that loads one exact Skill."""

    name = "load_skill"
    description = (
        "按精确名称加载一个已发现 Skill 的完整 SOP 和专属工具。"
        "调用前只能依据 Skill 目录中的 name 与 description 选择。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "arguments": {"type": "string"},
        },
        "required": ["name", "arguments"],
        "additionalProperties": False,
    }
    category = "read"

    def __init__(self, runtime: "SkillRuntime") -> None:
        self._runtime = runtime

    async def execute(self, arguments: dict[str, Any]) -> Any:
        validate_arguments(
            arguments,
            required={"name": str, "arguments": str},
        )
        try:
            return await self._runtime.load(
                arguments["name"],
                arguments["arguments"],
            )
        except SkillConfigError as exc:
            raise ToolExecutionError(exc.code, exc.message) from exc
