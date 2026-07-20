from __future__ import annotations

from pathlib import Path

import pytest

from mewcode_agent.notes import (
    NOTES_FILE_BYTES,
    NotesError,
    NotesSnapshot,
    load_notes,
    note_paths,
    render_project_notes,
    render_user_notes,
    write_note_scope,
)


def test_missing_note_files_load_as_empty_snapshot(tmp_path: Path) -> None:
    paths = note_paths(
        user_root=tmp_path / "user",
        project_root=tmp_path,
    )

    assert load_notes(paths=paths) == NotesSnapshot()


def test_generated_markdown_is_canonical_and_round_trips(
    tmp_path: Path,
) -> None:
    snapshot = NotesSnapshot(
        user_preferences=("默认中文回答",),
        correction_feedback=("不要改写用户原话",),
        project_knowledge=("入口是 src/main.py",),
        references=("docs/spec.md",),
    )
    paths = note_paths(
        user_root=tmp_path / "user",
        project_root=tmp_path,
    )

    write_note_scope(paths=paths, scope="user", snapshot=snapshot)
    write_note_scope(paths=paths, scope="project", snapshot=snapshot)

    assert paths.user.read_bytes() == (
        "# MewCode User Notes\n\n"
        "## 用户偏好\n\n"
        "- 默认中文回答\n\n"
        "## 纠正反馈\n\n"
        "- 不要改写用户原话\n"
    ).encode("utf-8")
    assert paths.project.read_bytes() == (
        "# MewCode Project Notes\n\n"
        "## 项目知识\n\n"
        "- 入口是 src/main.py\n\n"
        "## 参考资料\n\n"
        "- docs/spec.md\n"
    ).encode("utf-8")
    assert load_notes(paths=paths) == snapshot


def test_empty_markdown_has_exact_standard_shape() -> None:
    snapshot = NotesSnapshot()

    assert render_user_notes(snapshot) == (
        "# MewCode User Notes\n\n## 用户偏好\n\n## 纠正反馈\n"
    )
    assert render_project_notes(snapshot) == (
        "# MewCode Project Notes\n\n## 项目知识\n\n## 参考资料\n"
    )


@pytest.mark.parametrize(
    "content",
    [
        "# Wrong\n\n## 用户偏好\n\n## 纠正反馈\n",
        "# MewCode User Notes\n## 用户偏好\n\n## 纠正反馈\n",
        "# MewCode User Notes\n\n## 纠正反馈\n\n## 用户偏好\n",
        "# MewCode User Notes\n\n## 用户偏好\ntext\n\n## 纠正反馈\n",
        "# MewCode User Notes\n\n## 用户偏好\n\n- \n\n## 纠正反馈\n",
        "# MewCode User Notes\n\n## 用户偏好\n\n* wrong\n\n## 纠正反馈\n",
        "# MewCode User Notes\n\n## 用户偏好\n\n## 纠正反馈\n\n",
    ],
)
def test_manual_format_errors_are_rejected_without_repair(
    tmp_path: Path,
    content: str,
) -> None:
    paths = note_paths(
        user_root=tmp_path / "user",
        project_root=tmp_path,
    )
    paths.user.parent.mkdir()
    paths.user.write_text(content, encoding="utf-8", newline="")
    before = paths.user.read_bytes()

    with pytest.raises(NotesError) as captured:
        load_notes(paths=paths)

    assert captured.value.code == "notes_invalid_format"
    assert paths.user.read_bytes() == before


def test_crlf_and_missing_final_newline_are_accepted_for_manual_file(
    tmp_path: Path,
) -> None:
    paths = note_paths(
        user_root=tmp_path / "user",
        project_root=tmp_path,
    )
    paths.user.parent.mkdir()
    paths.user.write_bytes(
        b"# MewCode User Notes\r\n\r\n"
        b"## \xe7\x94\xa8\xe6\x88\xb7\xe5\x81\x8f\xe5\xa5\xbd\r\n\r\n"
        b"- concise\r\n\r\n"
        b"## \xe7\xba\xa0\xe6\xad\xa3\xe5\x8f\x8d\xe9\xa6\x88"
    )

    snapshot = load_notes(paths=paths)

    assert snapshot.user_preferences == ("concise",)
    assert snapshot.correction_feedback == ()


def test_note_entry_and_category_limits_are_enforced(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="无效条目"):
        NotesSnapshot(user_preferences=("x" * 1001,))
    with pytest.raises(ValueError, match="128"):
        NotesSnapshot(user_preferences=tuple("x" for _ in range(129)))

    paths = note_paths(
        user_root=tmp_path / "user",
        project_root=tmp_path,
    )
    paths.user.parent.mkdir()
    paths.user.write_text(
        "# MewCode User Notes\n\n## 用户偏好\n\n"
        + "".join(f"- item {index}\n" for index in range(129))
        + "\n## 纠正反馈\n",
        encoding="utf-8",
        newline="",
    )

    with pytest.raises(NotesError) as captured:
        load_notes(paths=paths)
    assert captured.value.code == "notes_invalid_format"


def test_file_size_and_invalid_utf8_are_rejected(tmp_path: Path) -> None:
    paths = note_paths(
        user_root=tmp_path / "user",
        project_root=tmp_path,
    )
    paths.user.parent.mkdir()
    paths.user.write_bytes(b"x" * (NOTES_FILE_BYTES + 1))
    with pytest.raises(NotesError) as too_large:
        load_notes(paths=paths)
    assert too_large.value.code == "notes_file_too_large"

    paths.user.write_bytes(b"# MewCode User Notes\n\xff")
    with pytest.raises(NotesError) as invalid_utf8:
        load_notes(paths=paths)
    assert invalid_utf8.value.code == "notes_invalid_format"


def test_write_failure_preserves_existing_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from mewcode_agent.notes import storage

    paths = note_paths(
        user_root=tmp_path / "user",
        project_root=tmp_path,
    )
    write_note_scope(
        paths=paths,
        scope="user",
        snapshot=NotesSnapshot(user_preferences=("old",)),
    )
    before = paths.user.read_bytes()
    monkeypatch.setattr(
        storage.os,
        "replace",
        lambda _source, _target: (_ for _ in ()).throw(OSError("SECRET")),
    )

    with pytest.raises(NotesError) as captured:
        write_note_scope(
            paths=paths,
            scope="user",
            snapshot=NotesSnapshot(user_preferences=("new",)),
        )

    assert captured.value.code == "notes_write_failed"
    assert "SECRET" not in str(captured.value)
    assert paths.user.read_bytes() == before


def test_project_notes_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    link = project / ".mewcode"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    paths = note_paths(
        user_root=tmp_path / "user",
        project_root=project,
    )

    with pytest.raises(NotesError) as captured:
        write_note_scope(
            paths=paths,
            scope="project",
            snapshot=NotesSnapshot(project_knowledge=("secret",)),
        )

    assert captured.value.code == "notes_write_failed"
    assert not (outside / "notes.md").exists()
