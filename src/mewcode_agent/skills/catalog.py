"""Deterministic three-layer Skill discovery and validation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from mewcode_agent.skills.loader import load_skill_definition
from mewcode_agent.skills.models import (
    SkillCatalogSnapshot,
    SkillConfigError,
    SkillDefinition,
    SkillDiagnostic,
    SkillSource,
)


SkillDiagnosticHandler = Callable[[SkillDiagnostic], None]


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: Path
    label: str
    directory_skill: bool


def builtin_skill_root() -> Path:
    """Return the installed package resource directory for built-in Skills."""

    root = resources.files("mewcode_agent.builtin_skills")
    path = Path(str(root))
    if not path.is_dir():
        raise SkillConfigError("skill_path_invalid", "内置 Skill 目录不可用")
    return path.resolve(strict=True)


def _candidates(root: Path) -> tuple[_Candidate, ...]:
    if not root.exists():
        return ()
    if not root.is_dir():
        raise SkillConfigError("skill_path_invalid", "Skill 根路径不是目录")
    found: list[_Candidate] = []
    try:
        children = tuple(root.iterdir())
    except OSError as exc:
        raise SkillConfigError("skill_path_invalid", "无法扫描 Skill 根目录") from exc
    for child in children:
        if child.is_file() and child.suffix == ".md":
            found.append(_Candidate(child, child.name, False))
        elif child.is_dir() and (child / "SKILL.md").is_file():
            found.append(_Candidate(child / "SKILL.md", f"{child.name}/SKILL.md", True))
    return tuple(sorted(found, key=lambda item: item.label))


def _scan_layer(
    root: Path,
    source: SkillSource,
) -> tuple[dict[str, SkillDefinition], list[SkillDiagnostic]]:
    if not root.exists():
        return {}, []
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise SkillConfigError("skill_path_invalid", "无法解析 Skill 根目录") from exc
    definitions_by_name: dict[str, list[tuple[SkillDefinition, str]]] = {}
    diagnostics: list[SkillDiagnostic] = []
    for candidate in _candidates(resolved_root):
        try:
            definition = load_skill_definition(
                candidate.path,
                source=source,
                source_root=resolved_root,
                directory_skill=candidate.directory_skill,
            )
        except SkillConfigError as exc:
            diagnostics.append(
                SkillDiagnostic(source, candidate.label, exc.code, exc.message)
            )
            continue
        definitions_by_name.setdefault(definition.name, []).append(
            (definition, candidate.label)
        )
    valid: dict[str, SkillDefinition] = {}
    for name, candidates in definitions_by_name.items():
        if len(candidates) == 1:
            valid[name] = candidates[0][0]
            continue
        for _, label in candidates:
            diagnostics.append(
                SkillDiagnostic(
                    source,
                    label,
                    "skill_name_conflict",
                    f"同一来源存在重复 Skill 名称: {name}",
                )
            )
    return valid, diagnostics


def _validate_final(
    definitions: Iterable[SkillDefinition],
    *,
    existing_tool_names: frozenset[str],
    reserved_command_names: frozenset[str],
) -> None:
    definitions = tuple(definitions)
    dedicated_names: set[str] = set()
    for definition in definitions:
        if definition.name in reserved_command_names:
            raise SkillConfigError(
                "skill_name_conflict",
                f"Skill 名称与内置命令或别名冲突: {definition.name}",
            )
        for tool in definition.dedicated_tools:
            if tool.name in existing_tool_names or tool.name in dedicated_names:
                raise SkillConfigError(
                    "skill_tool_conflict",
                    f"Skill 专属工具名冲突: {tool.name}",
                )
            if tool.name == "load_skill" or tool.name.startswith("mcp_"):
                raise SkillConfigError(
                    "skill_tool_conflict",
                    f"Skill 专属工具使用保留名称: {tool.name}",
                )
            dedicated_names.add(tool.name)
    available = existing_tool_names | dedicated_names | {"load_skill"}
    for definition in definitions:
        missing = tuple(
            name for name in definition.allowed_tools if name not in available
        )
        if missing:
            raise SkillConfigError(
                "skill_tool_missing",
                f"Skill {definition.name} 引用了不存在的工具: {', '.join(missing)}",
            )


def scan_skill_catalog(
    *,
    project_root: Path,
    user_root: Path,
    builtin_root: Path | None = None,
    existing_tool_names: Iterable[str],
    reserved_command_names: Iterable[str],
    diagnostic_handler: SkillDiagnosticHandler | None = None,
) -> SkillCatalogSnapshot:
    """Scan all layers, apply precedence, then enforce global invariants."""

    roots: tuple[tuple[SkillSource, Path], ...] = (
        ("builtin", builtin_root or builtin_skill_root()),
        ("user", user_root / "skills"),
        ("project", project_root / ".mewcode" / "skills"),
    )
    selected: dict[str, SkillDefinition] = {}
    diagnostics: list[SkillDiagnostic] = []
    for source, root in roots:
        layer, layer_diagnostics = _scan_layer(root, source)
        selected.update(layer)
        diagnostics.extend(layer_diagnostics)
    definitions = tuple(selected[name] for name in sorted(selected))
    _validate_final(
        definitions,
        existing_tool_names=frozenset(existing_tool_names),
        reserved_command_names=frozenset(reserved_command_names),
    )
    snapshot = SkillCatalogSnapshot(definitions, tuple(diagnostics))
    if diagnostic_handler is not None:
        for diagnostic in snapshot.diagnostics:
            diagnostic_handler(diagnostic)
    return snapshot


class SkillCatalog:
    """Own one atomically replaceable, name-indexed Skill snapshot."""

    def __init__(self, snapshot: SkillCatalogSnapshot) -> None:
        if not isinstance(snapshot, SkillCatalogSnapshot):
            raise ValueError("snapshot 类型无效")
        self._snapshot = snapshot
        self._by_name = {item.name: item for item in snapshot.definitions}

    @property
    def snapshot(self) -> SkillCatalogSnapshot:
        return self._snapshot

    def get(self, name: str) -> SkillDefinition | None:
        if not isinstance(name, str):
            raise TypeError("name 必须是字符串")
        return self._by_name.get(name)

    def replace(self, snapshot: SkillCatalogSnapshot) -> None:
        if not isinstance(snapshot, SkillCatalogSnapshot):
            raise ValueError("snapshot 类型无效")
        replacement = {item.name: item for item in snapshot.definitions}
        self._snapshot = snapshot
        self._by_name = replacement

    def reload(self, name: str) -> SkillDefinition:
        current = self.get(name)
        if current is None:
            raise SkillConfigError("skill_not_found", "Skill 不存在")
        refreshed = load_skill_definition(
            current.source_path,
            source=current.source,
            source_root=current.source_root,
            directory_skill=current.skill_directory is not None,
        )
        if refreshed.name != name:
            raise SkillConfigError(
                "skill_source_changed",
                "Skill 源文件名称已改变，请执行 /skills rescan",
            )
        return refreshed
