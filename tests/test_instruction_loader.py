from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.instructions import (
    INSTRUCTION_FILE_BYTES,
    InstructionConfigError,
    load_instruction_documents,
)


def test_missing_and_blank_layers_do_not_create_documents(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    user_root = tmp_path / "user"
    project_root.mkdir()
    user_root.mkdir()
    (project_root / "MEWCODE.md").write_text(" \r\n\t", encoding="utf-8")

    assert load_instruction_documents(
        user_root=user_root,
        project_root=project_root,
    ) == ()


def test_loads_project_before_user_and_expands_include_in_place(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    user_root = tmp_path / "user"
    (project_root / "rules").mkdir(parents=True)
    user_root.mkdir()
    (project_root / "MEWCODE.md").write_text(
        "project before\n  @include <rules/python.md>  \nproject after",
        encoding="utf-8",
    )
    (project_root / "rules" / "python.md").write_text(
        "included rule\n",
        encoding="utf-8",
    )
    (user_root / "INSTRUCTIONS.md").write_text(
        "user rule",
        encoding="utf-8",
    )

    documents = load_instruction_documents(
        user_root=user_root,
        project_root=project_root,
    )

    assert [item.layer for item in documents] == ["project", "user"]
    assert documents[0].content == (
        "project before\nincluded rule\nproject after\n"
    )
    assert documents[1].content == "user rule\n"
    assert [item.to_runtime_instruction().instruction_id for item in documents] == [
        "runtime.instructions.project",
        "runtime.instructions.user",
    ]


def test_nonexclusive_include_text_is_not_expanded(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "MEWCODE.md").write_text(
        "正文中的 @include <missing.md> 不展开",
        encoding="utf-8",
    )

    (document,) = load_instruction_documents(
        user_root=tmp_path / "missing-user",
        project_root=project_root,
    )

    assert document.content == "正文中的 @include <missing.md> 不展开\n"


@pytest.mark.parametrize(
    ("directive", "code", "relative_path"),
    [
        (
            "@include missing.md",
            "instruction_include_invalid",
            "MEWCODE.md",
        ),
        ("@include <>", "instruction_include_invalid", "MEWCODE.md"),
        (
            "@include <x.md> trailing",
            "instruction_include_invalid",
            "MEWCODE.md",
        ),
        (
            "@include <missing.md>",
            "instruction_include_not_found",
            "missing.md",
        ),
        ("@include <folder>", "instruction_include_not_found", "folder"),
        (
            "@include <../outside.md>",
            "instruction_include_outside_root",
            "MEWCODE.md",
        ),
        (
            "@include <C:\\outside.md>",
            "instruction_include_outside_root",
            "MEWCODE.md",
        ),
        (
            "@include </outside.md>",
            "instruction_include_outside_root",
            "MEWCODE.md",
        ),
    ],
)
def test_include_validation_returns_stable_content_free_error(
    tmp_path: Path,
    directive: str,
    code: str,
    relative_path: str,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "folder").mkdir(parents=True)
    (tmp_path / "outside.md").write_text("SECRET_OUTSIDE", encoding="utf-8")
    (project_root / "MEWCODE.md").write_text(directive, encoding="utf-8")

    with pytest.raises(InstructionConfigError) as captured:
        load_instruction_documents(
            user_root=tmp_path / "user",
            project_root=project_root,
        )

    assert captured.value.code == code
    assert captured.value.layer == "project"
    assert captured.value.relative_path == relative_path
    assert "SECRET_OUTSIDE" not in str(captured.value)


def test_include_cycle_is_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "MEWCODE.md").write_text(
        "@include <child.md>",
        encoding="utf-8",
    )
    (project_root / "child.md").write_text(
        "@include <MEWCODE.md>",
        encoding="utf-8",
    )

    with pytest.raises(InstructionConfigError) as captured:
        load_instruction_documents(
            user_root=tmp_path / "user",
            project_root=project_root,
        )

    assert captured.value.code == "instruction_include_cycle"
    assert captured.value.relative_path == "MEWCODE.md"


def test_missing_parent_traversal_is_still_rejected_as_outside_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "MEWCODE.md").write_text(
        "@include <../does-not-exist.md>",
        encoding="utf-8",
    )

    with pytest.raises(InstructionConfigError) as captured:
        load_instruction_documents(
            user_root=tmp_path / "user",
            project_root=project_root,
        )

    assert captured.value.code == "instruction_include_outside_root"


def test_include_depth_five_is_allowed(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    paths = [project_root / "MEWCODE.md"] + [
        project_root / f"level_{index}.md" for index in range(1, 6)
    ]
    for index, path in enumerate(paths[:-1], start=1):
        path.write_text(
            f"@include <{paths[index].name}>",
            encoding="utf-8",
        )
    paths[-1].write_text("depth five", encoding="utf-8")

    (document,) = load_instruction_documents(
        user_root=tmp_path / "user",
        project_root=project_root,
    )

    assert document.content == "depth five\n"


def test_include_depth_six_is_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    paths = [project_root / "MEWCODE.md"] + [
        project_root / f"level_{index}.md" for index in range(1, 7)
    ]
    for index, path in enumerate(paths[:-1], start=1):
        path.write_text(
            f"@include <{paths[index].name}>",
            encoding="utf-8",
        )
    paths[-1].write_text("too deep", encoding="utf-8")

    with pytest.raises(InstructionConfigError) as captured:
        load_instruction_documents(
            user_root=tmp_path / "user",
            project_root=project_root,
        )

    assert captured.value.code == "instruction_include_depth_exceeded"
    assert captured.value.relative_path == "level_6.md"


def test_invalid_utf8_is_rejected_without_exposing_content(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "MEWCODE.md").write_bytes(b"SECRET\xff")

    with pytest.raises(InstructionConfigError) as captured:
        load_instruction_documents(
            user_root=tmp_path / "user",
            project_root=project_root,
        )

    assert captured.value.code == "instruction_invalid_utf8"
    assert "SECRET" not in str(captured.value)


def test_single_file_larger_than_limit_is_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "MEWCODE.md").write_bytes(
        b"x" * (INSTRUCTION_FILE_BYTES + 1)
    )

    with pytest.raises(InstructionConfigError) as captured:
        load_instruction_documents(
            user_root=tmp_path / "user",
            project_root=project_root,
        )

    assert captured.value.code == "instruction_file_too_large"


def test_expanded_layer_larger_than_limit_is_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "chunk.md").write_bytes(b"x" * INSTRUCTION_FILE_BYTES)
    (project_root / "MEWCODE.md").write_text(
        "@include <chunk.md>\n" * 4,
        encoding="utf-8",
    )

    with pytest.raises(InstructionConfigError) as captured:
        load_instruction_documents(
            user_root=tmp_path / "user",
            project_root=project_root,
        )

    assert captured.value.code == "instruction_total_too_large"


def test_symlink_outside_root_is_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = project_root / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    (project_root / "MEWCODE.md").write_text(
        "@include <linked.md>",
        encoding="utf-8",
    )

    with pytest.raises(InstructionConfigError) as captured:
        load_instruction_documents(
            user_root=tmp_path / "user",
            project_root=project_root,
        )

    assert captured.value.code == "instruction_include_outside_root"


def test_entry_directory_is_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "MEWCODE.md").mkdir(parents=True)

    with pytest.raises(InstructionConfigError) as captured:
        load_instruction_documents(
            user_root=tmp_path / "user",
            project_root=project_root,
        )

    assert captured.value.code == "instruction_read_failed"
