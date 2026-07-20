from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.workers import (
    WorkerCatalog,
    WorkerConfigError,
    builtin_worker_root,
    scan_worker_catalog,
)

from test_worker_loader import worker_document, write_text


DEFAULT_TOOLS = frozenset(
    {
        "read_file",
        "find_files",
        "search_code",
        "read_context_artifact",
        "run_command",
    }
)


def scan(
    tmp_path: Path,
    *,
    builtin_root: Path | None = None,
    plugin_roots: tuple[Path, ...] = (),
    tools: frozenset[str] = DEFAULT_TOOLS,
) -> WorkerCatalog:
    snapshot = scan_worker_catalog(
        project_root=tmp_path / "project",
        user_root=tmp_path / "user",
        existing_tool_names=tools,
        provider_ids=("provider-a",),
        builtin_root=builtin_root,
        plugin_roots=plugin_roots,
    )
    return WorkerCatalog(snapshot)


def test_builtin_catalog_exposes_three_default_roles(tmp_path: Path) -> None:
    catalog = scan(tmp_path)

    assert tuple(
        definition.name for definition in catalog.snapshot.definitions
    ) == ("explore", "general", "plan")
    assert catalog.get("verify") is None
    assert catalog.get("Explore") is None
    assert catalog.get("explore") is not None
    assert builtin_worker_root().is_absolute()


def test_project_overrides_user_builtin_and_plugin(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    builtin = tmp_path / "builtin"
    user = tmp_path / "user" / "workers"
    project = tmp_path / "project" / ".mewcode" / "workers"
    for root, description in (
        (plugin, "plugin"),
        (builtin, "builtin"),
        (user, "user"),
        (project, "project"),
    ):
        write_text(
            root / f"{description}.md",
            worker_document(description=description),
        )

    catalog = scan(
        tmp_path,
        builtin_root=builtin,
        plugin_roots=(plugin.resolve(),),
    )

    definition = catalog.get("example")
    assert definition is not None
    assert definition.source == "project"
    assert definition.description == "project"


def test_invalid_high_priority_candidate_falls_back_and_reports_diagnostic(
    tmp_path: Path,
) -> None:
    builtin = tmp_path / "builtin"
    project = tmp_path / "project" / ".mewcode" / "workers"
    write_text(builtin / "valid.md", worker_document(description="builtin"))
    write_text(project / "broken.md", "not frontmatter\n")

    catalog = scan(tmp_path, builtin_root=builtin)

    definition = catalog.get("example")
    assert definition is not None
    assert definition.source == "builtin"
    assert len(catalog.snapshot.diagnostics) == 1
    diagnostic = catalog.snapshot.diagnostics[0]
    assert diagnostic.source == "project"
    assert diagnostic.candidate == "broken.md"
    assert diagnostic.code == "worker_document_invalid"
    assert "not frontmatter" not in diagnostic.message


def test_same_layer_name_conflict_invalidates_all_candidates(
    tmp_path: Path,
) -> None:
    builtin = tmp_path / "builtin"
    project = tmp_path / "project" / ".mewcode" / "workers"
    write_text(builtin / "fallback.md", worker_document(description="fallback"))
    write_text(project / "one.md", worker_document(description="one"))
    write_text(project / "two.md", worker_document(description="two"))

    catalog = scan(tmp_path, builtin_root=builtin)

    definition = catalog.get("example")
    assert definition is not None
    assert definition.description == "fallback"
    assert [item.code for item in catalog.snapshot.diagnostics] == [
        "worker_name_conflict",
        "worker_name_conflict",
    ]


def test_plugin_conflict_does_not_select_registration_order(
    tmp_path: Path,
) -> None:
    first = tmp_path / "plugin-one"
    second = tmp_path / "plugin-two"
    builtin = tmp_path / "builtin"
    write_text(first / "one.md", worker_document(description="one"))
    write_text(second / "two.md", worker_document(description="two"))
    builtin.mkdir()

    catalog = scan(
        tmp_path,
        builtin_root=builtin,
        plugin_roots=(first.resolve(), second.resolve()),
    )

    assert catalog.get("example") is None
    assert len(catalog.snapshot.diagnostics) == 2
    assert {
        item.candidate for item in catalog.snapshot.diagnostics
    } == {"plugin[0]/one.md", "plugin[1]/two.md"}


def test_verify_flag_only_hides_builtin_candidate(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    write_text(
        builtin / "verify.md",
        worker_document(name="verify", description="builtin verify"),
    )
    write_text(
        user / "workers" / "verify.md",
        worker_document(name="verify", description="user verify"),
    )

    catalog = scan(tmp_path, builtin_root=builtin)

    definition = catalog.get("verify")
    assert definition is not None
    assert definition.source == "user"

    write_text(
        user / "workers.yaml",
        """version: 1
max_concurrency: 4
foreground_timeout_seconds: 15
background_allowed_tools:
  - read_file
enable_verify_role: true
""",
    )
    catalog = scan(tmp_path, builtin_root=builtin)
    enabled_definition = catalog.get("verify")
    assert enabled_definition is not None
    assert enabled_definition.source == "user"


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (
            worker_document(allowed_tools="\n  - missing_tool"),
            "worker_tool_missing",
        ),
        (
            worker_document(denied_tools="\n  - missing_tool"),
            "worker_tool_missing",
        ),
        (
            worker_document(model="missing-provider"),
            "worker_model_missing",
        ),
    ],
)
def test_final_references_fail_fast(
    tmp_path: Path,
    document: str,
    code: str,
) -> None:
    builtin = tmp_path / "builtin"
    write_text(builtin / "example.md", document)

    with pytest.raises(WorkerConfigError) as caught:
        scan(tmp_path, builtin_root=builtin)

    assert caught.value.code == code


def test_background_allowlist_references_fail_fast(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    write_text(
        tmp_path / "user" / "workers.yaml",
        """version: 1
max_concurrency: 4
foreground_timeout_seconds: 15
background_allowed_tools:
  - missing_tool
enable_verify_role: false
""",
    )

    with pytest.raises(WorkerConfigError) as caught:
        scan(tmp_path, builtin_root=builtin)

    assert caught.value.code == "worker_tool_missing"


def test_rejects_relative_plugin_root(tmp_path: Path) -> None:
    with pytest.raises(WorkerConfigError) as caught:
        scan(tmp_path, plugin_roots=(Path("relative"),))

    assert caught.value.code == "worker_document_invalid"


def test_scans_only_direct_lowercase_markdown_files(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    write_text(
        builtin / "direct.md",
        worker_document(name="direct", description="direct"),
    )
    write_text(
        builtin / "upper.MD",
        worker_document(name="upper", description="upper"),
    )
    write_text(
        builtin / "nested" / "nested.md",
        worker_document(name="nested", description="nested"),
    )

    catalog = scan(tmp_path, builtin_root=builtin)

    assert tuple(
        definition.name for definition in catalog.snapshot.definitions
    ) == ("direct",)
