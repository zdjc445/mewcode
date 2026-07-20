"""Layered discovery and validation for worker role definitions."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from importlib import resources
from pathlib import Path

from mewcode_agent.workers.loader import (
    load_worker_role,
    load_worker_runtime_config,
)
from mewcode_agent.workers.models import (
    WorkerCatalogSnapshot,
    WorkerConfigError,
    WorkerDiagnostic,
    WorkerRoleDefinition,
    WorkerSource,
)


WorkerDiagnosticHandler = Callable[[WorkerDiagnostic], None]


def builtin_worker_root() -> Path:
    root = Path(str(resources.files("mewcode_agent.builtin_workers")))
    if not root.is_dir():
        raise WorkerConfigError(
            "worker_document_invalid",
            "内置 Worker 目录不可用",
        )
    return root.resolve(strict=True)


def _candidates(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    if not root.is_dir():
        raise WorkerConfigError(
            "worker_document_invalid",
            "Worker 根路径不是目录",
        )
    try:
        return tuple(
            sorted(
                (
                    child
                    for child in root.iterdir()
                    if child.is_file() and child.suffix == ".md"
                ),
                key=lambda child: child.name,
            )
        )
    except OSError as exc:
        raise WorkerConfigError(
            "worker_document_invalid",
            "无法扫描 Worker 根目录",
        ) from exc


def _scan_layer(
    roots: tuple[Path, ...],
    *,
    source: WorkerSource,
    enable_builtin_verify: bool,
) -> tuple[dict[str, WorkerRoleDefinition], list[WorkerDiagnostic]]:
    definitions_by_name: dict[
        str,
        list[tuple[WorkerRoleDefinition, str]],
    ] = {}
    diagnostics: list[WorkerDiagnostic] = []
    for root_index, root in enumerate(roots):
        if not root.exists():
            continue
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise WorkerConfigError(
                "worker_document_invalid",
                "无法解析 Worker 根目录",
            ) from exc
        for candidate in _candidates(resolved_root):
            label = (
                f"plugin[{root_index}]/{candidate.name}"
                if source == "plugin"
                else candidate.name
            )
            try:
                definition = load_worker_role(
                    candidate,
                    source=source,
                    source_root=resolved_root,
                )
            except WorkerConfigError as exc:
                diagnostics.append(
                    WorkerDiagnostic(
                        source,
                        label,
                        exc.code,
                        exc.message,
                    )
                )
                continue
            if (
                source == "builtin"
                and definition.name == "verify"
                and not enable_builtin_verify
            ):
                continue
            definitions_by_name.setdefault(definition.name, []).append(
                (definition, label)
            )
    valid: dict[str, WorkerRoleDefinition] = {}
    for name, candidates in definitions_by_name.items():
        if len(candidates) == 1:
            valid[name] = candidates[0][0]
            continue
        for _, label in candidates:
            diagnostics.append(
                WorkerDiagnostic(
                    source,
                    label,
                    "worker_name_conflict",
                    f"同一来源存在重复 Worker 名称: {name}",
                )
            )
    return valid, diagnostics


def validate_worker_definitions(
    definitions: Iterable[WorkerRoleDefinition],
    *,
    existing_tool_names: frozenset[str],
    provider_ids: frozenset[str],
    background_allowed_tools: tuple[str, ...],
) -> None:
    available_tools = existing_tool_names | {"spawn_worker"}
    missing_background = tuple(
        name
        for name in background_allowed_tools
        if name not in available_tools
    )
    if missing_background:
        raise WorkerConfigError(
            "worker_tool_missing",
            "后台 Worker 白名单引用不存在的工具: "
            + ", ".join(missing_background),
        )
    for definition in definitions:
        if definition.model != "inherit" and definition.model not in provider_ids:
            raise WorkerConfigError(
                "worker_model_missing",
                f"Worker {definition.name} 引用了不存在的 Provider: "
                f"{definition.model}",
            )
        references = (
            *(definition.allowed_tools or ()),
            *definition.denied_tools,
        )
        missing = tuple(
            name for name in references if name not in available_tools
        )
        if missing:
            raise WorkerConfigError(
                "worker_tool_missing",
                f"Worker {definition.name} 引用了不存在的工具: "
                + ", ".join(missing),
            )


def scan_worker_catalog(
    *,
    project_root: Path,
    user_root: Path,
    existing_tool_names: Iterable[str],
    provider_ids: Iterable[str],
    builtin_root: Path | None = None,
    plugin_roots: tuple[Path, ...] = (),
    diagnostic_handler: WorkerDiagnosticHandler | None = None,
) -> WorkerCatalogSnapshot:
    if any(
        not isinstance(root, Path) or not root.is_absolute()
        for root in plugin_roots
    ):
        raise WorkerConfigError(
            "worker_document_invalid",
            "插件 Worker 根目录必须是绝对 Path",
        )
    runtime = load_worker_runtime_config(user_root / "workers.yaml")
    layers: tuple[tuple[WorkerSource, tuple[Path, ...]], ...] = (
        ("plugin", plugin_roots),
        ("builtin", (builtin_root or builtin_worker_root(),)),
        ("user", (user_root / "workers",)),
        ("project", (project_root / ".mewcode" / "workers",)),
    )
    selected: dict[str, WorkerRoleDefinition] = {}
    diagnostics: list[WorkerDiagnostic] = []
    for source, roots in layers:
        layer, layer_diagnostics = _scan_layer(
            roots,
            source=source,
            enable_builtin_verify=runtime.enable_verify_role,
        )
        selected.update(layer)
        diagnostics.extend(layer_diagnostics)
    definitions = tuple(selected[name] for name in sorted(selected))
    validate_worker_definitions(
        definitions,
        existing_tool_names=frozenset(existing_tool_names),
        provider_ids=frozenset(provider_ids),
        background_allowed_tools=runtime.background_allowed_tools,
    )
    snapshot = WorkerCatalogSnapshot(
        definitions,
        tuple(diagnostics),
        runtime,
    )
    if diagnostic_handler is not None:
        for diagnostic in snapshot.diagnostics:
            diagnostic_handler(diagnostic)
    return snapshot


class WorkerCatalog:
    def __init__(self, snapshot: WorkerCatalogSnapshot) -> None:
        if not isinstance(snapshot, WorkerCatalogSnapshot):
            raise ValueError("snapshot 类型无效")
        self._snapshot = snapshot
        self._by_name = {
            definition.name: definition
            for definition in snapshot.definitions
        }

    @property
    def snapshot(self) -> WorkerCatalogSnapshot:
        return self._snapshot

    def get(self, name: str) -> WorkerRoleDefinition | None:
        if not isinstance(name, str):
            raise TypeError("name 必须是字符串")
        return self._by_name.get(name)
