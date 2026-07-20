from __future__ import annotations

import os
from pathlib import Path

import pytest

from mewcode_agent.compaction import (
    CompactionConfig,
    ContextArtifactStore,
    ContextCompactionError,
)
from mewcode_agent.tools import ReadContextArtifactTool, create_core_registry


SESSION_ID = "a" * 32


def artifact_config(**overrides: object) -> CompactionConfig:
    values: dict[str, object] = {
        "single_tool_result_bytes": 128,
        "tool_batch_bytes": 256,
        "preview_bytes": 32,
        "preview_head_bytes": 24,
        "preview_tail_bytes": 8,
        "artifact_bytes": 1024,
        "artifact_session_bytes": 2048,
    }
    values.update(overrides)
    return CompactionConfig(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_artifact_store_writes_deduplicates_reads_and_cleans(
    tmp_path: Path,
) -> None:
    store = ContextArtifactStore(
        root=tmp_path / "artifacts",
        session_id=SESSION_ID,
        config=artifact_config(),
    )
    content = '{"value":"第一行\\nsecond"}'

    first = await store.write(content)
    second = await store.write(content)
    page = await store.read(str(first.path), offset=2, limit=5)

    assert first == second
    assert first.path.name == f"{first.sha256}.json"
    assert first.path.read_text(encoding="utf-8") == content
    assert page == {
        "path": str(first.path),
        "sha256": first.sha256,
        "content": content[2:7],
        "offset": 2,
        "limit": 5,
        "total_characters": len(content),
        "has_more": True,
        "next_offset": 7,
    }

    await store.close()

    assert not store.session_directory.exists()


@pytest.mark.asyncio
async def test_artifact_store_rejects_unregistered_and_corrupted_paths(
    tmp_path: Path,
) -> None:
    store = ContextArtifactStore(
        root=tmp_path / "artifacts",
        session_id=SESSION_ID,
        config=artifact_config(),
    )
    reference = await store.write('{"value":1}')

    with pytest.raises(
        ContextCompactionError,
        match="未获授权",
    ) as denied:
        await store.read(str(tmp_path / "other.json"), offset=0, limit=10)
    assert denied.value.code == "context_artifact_access_denied"

    reference.path.write_text('{"value":2}', encoding="utf-8")
    with pytest.raises(
        ContextCompactionError,
        match="摘要校验失败",
    ) as corrupted:
        await store.read(str(reference.path), offset=0, limit=10)
    assert corrupted.value.code == "context_artifact_corrupted"

    await store.close()


@pytest.mark.asyncio
async def test_artifact_store_enforces_file_and_session_limits(
    tmp_path: Path,
) -> None:
    store = ContextArtifactStore(
        root=tmp_path / "artifacts",
        session_id=SESSION_ID,
        config=artifact_config(
            artifact_bytes=40,
            artifact_session_bytes=60,
        ),
    )

    with pytest.raises(ContextCompactionError) as oversized:
        await store.write("x" * 41)
    assert oversized.value.code == "context_artifact_too_large"

    await store.write("a" * 35)
    with pytest.raises(ContextCompactionError) as exhausted:
        await store.write("b" * 30)
    assert exhausted.value.code == "context_artifact_budget_exceeded"

    await store.close()


@pytest.mark.asyncio
async def test_artifact_store_only_cleans_exact_stale_session_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    stale = root / ("b" * 32)
    recent = root / ("c" * 32)
    unrelated = root / "keep-me"
    for path in (stale, recent, unrelated):
        path.mkdir(parents=True)
        (path / "value.txt").write_text("value", encoding="utf-8")
    os.utime(stale, (0, 0))
    os.utime(recent, (100000, 100000))

    store = ContextArtifactStore(
        root=root,
        session_id=SESSION_ID,
        config=artifact_config(stale_artifact_seconds=100),
    )
    await store.cleanup_stale(now=100050)

    assert not stale.exists()
    assert recent.exists()
    assert unrelated.exists()


@pytest.mark.asyncio
async def test_read_context_artifact_tool_uses_registered_store(
    tmp_path: Path,
) -> None:
    store = ContextArtifactStore(
        root=tmp_path / "artifacts",
        session_id=SESSION_ID,
        config=artifact_config(),
    )
    reference = await store.write('{"value":"content"}')
    registry = create_core_registry(
        working_directory=tmp_path,
        artifact_store=store,
    )

    tool = registry.get("read_context_artifact")
    assert isinstance(tool, ReadContextArtifactTool)
    result = await registry.execute(
        "read_context_artifact",
        (
            '{"path":"'
            + str(reference.path).replace("\\", "\\\\")
            + '","offset":0,"limit":8}'
        ),
    )

    assert result.success is True
    assert result.data["content"] == '{"value"'
    assert result.data["has_more"] is True

    await store.close()
