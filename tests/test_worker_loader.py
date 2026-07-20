from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.workers import (
    WorkerConfigError,
    WorkerRuntimeConfig,
    load_worker_role,
    load_worker_runtime_config,
)


def worker_document(
    *,
    name: str = "example",
    description: str = "Example worker",
    allowed_tools: str = "\n  - read_file",
    denied_tools: str = "[]",
    model: str = "inherit",
    max_rounds: str = "12",
    permission_mode: str = "inherit",
    isolation: str = "none",
    body: str = "Follow this worker SOP.",
) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"allowed_tools:{allowed_tools}\n"
        f"denied_tools: {denied_tools}\n"
        f"model: {model}\n"
        f"max_rounds: {max_rounds}\n"
        f"permission_mode: {permission_mode}\n"
        f"isolation: {isolation}\n"
        "---\n"
        f"{body}\n"
    )


def write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_exact_worker_metadata_and_body(tmp_path: Path) -> None:
    root = tmp_path / "workers"
    path = write_text(root / "alias.md", worker_document())

    definition = load_worker_role(
        path,
        source="project",
        source_root=root,
    )

    assert definition.name == "example"
    assert definition.description == "Example worker"
    assert definition.allowed_tools == ("read_file",)
    assert definition.denied_tools == ()
    assert definition.model == "inherit"
    assert definition.max_rounds == 12
    assert definition.permission_mode == "inherit"
    assert definition.isolation == "none"
    assert definition.body == "Follow this worker SOP."
    assert definition.source == "project"
    assert definition.source_root == root.resolve()
    assert definition.source_path == path.resolve()


def test_loads_crlf_and_nullable_allowed_tools(tmp_path: Path) -> None:
    root = tmp_path / "workers"
    content = worker_document(
        allowed_tools=" null",
        denied_tools="\n  - spawn_worker",
        permission_mode="strict",
        isolation="worktree",
    ).replace("\n", "\r\n")
    path = write_text(root / "example.md", content)

    definition = load_worker_role(
        path,
        source="user",
        source_root=root,
    )

    assert definition.allowed_tools is None
    assert definition.denied_tools == ("spawn_worker",)
    assert definition.permission_mode == "strict"
    assert definition.isolation == "worktree"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("name: example\n", "worker_document_invalid"),
        ("---\nname: example\n", "worker_document_invalid"),
        (worker_document(body="   "), "worker_document_invalid"),
        (
            worker_document().replace(
                "description: Example worker\n",
                "description: one\ndescription: two\n",
            ),
            "worker_document_invalid",
        ),
        (
            worker_document().replace(
                "description: Example worker\n",
                "description: Example worker\nunknown: true\n",
            ),
            "worker_metadata_invalid",
        ),
        (worker_document(name="Example"), "worker_metadata_invalid"),
        (worker_document(allowed_tools="\n  - spawn_worker"), "worker_metadata_invalid"),
        (
            worker_document(
                allowed_tools="\n  - read_file",
                denied_tools="\n  - read_file",
            ),
            "worker_metadata_invalid",
        ),
        (worker_document(max_rounds="true"), "worker_metadata_invalid"),
        (worker_document(max_rounds="31"), "worker_metadata_invalid"),
        (
            worker_document(permission_mode="ask"),
            "worker_metadata_invalid",
        ),
        (
            worker_document(isolation="container"),
            "worker_metadata_invalid",
        ),
    ],
)
def test_rejects_invalid_worker_documents(
    tmp_path: Path,
    content: str,
    code: str,
) -> None:
    root = tmp_path / "workers"
    path = write_text(root / "example.md", content)

    with pytest.raises(WorkerConfigError) as caught:
        load_worker_role(
            path,
            source="project",
            source_root=root,
        )

    assert caught.value.code == code


def test_runtime_config_uses_defaults_without_creating_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workers.yaml"

    config = load_worker_runtime_config(path)

    assert config == WorkerRuntimeConfig()
    assert not path.exists()


def test_loads_exact_runtime_config(tmp_path: Path) -> None:
    path = write_text(
        tmp_path / "workers.yaml",
        """version: 1
max_concurrency: 8
foreground_timeout_seconds: 2.5
background_allowed_tools:
  - read_file
enable_verify_role: true
""",
    )

    config = load_worker_runtime_config(path)

    assert config.max_concurrency == 8
    assert config.foreground_timeout_seconds == 2.5
    assert config.background_allowed_tools == ("read_file",)
    assert config.enable_verify_role is True


@pytest.mark.parametrize(
    "content",
    [
        "version: 1\n",
        """version: 1
max_concurrency: 4
foreground_timeout_seconds: 15
background_allowed_tools: [read_file]
enable_verify_role: false
unknown: true
""",
        """version: 2
max_concurrency: 4
foreground_timeout_seconds: 15
background_allowed_tools: [read_file]
enable_verify_role: false
""",
        """version: 1
max_concurrency: true
foreground_timeout_seconds: 15
background_allowed_tools: [read_file]
enable_verify_role: false
""",
        """version: 1
max_concurrency: 4
foreground_timeout_seconds: .inf
background_allowed_tools: [read_file]
enable_verify_role: false
""",
        """version: 1
max_concurrency: 4
foreground_timeout_seconds: 15
background_allowed_tools: []
enable_verify_role: false
""",
        """version: 1
max_concurrency: 4
foreground_timeout_seconds: 15
background_allowed_tools: [spawn_worker]
enable_verify_role: false
""",
        """version: 1
max_concurrency: 4
foreground_timeout_seconds: 15
background_allowed_tools: [read_file]
enable_verify_role: "false"
""",
    ],
)
def test_rejects_invalid_runtime_config(
    tmp_path: Path,
    content: str,
) -> None:
    path = write_text(tmp_path / "workers.yaml", content)

    with pytest.raises(WorkerConfigError) as caught:
        load_worker_runtime_config(path)

    assert caught.value.code == "worker_config_invalid"
