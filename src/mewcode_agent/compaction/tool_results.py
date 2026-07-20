"""Preventive externalization of oversized tool-result batches."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from mewcode_agent.compaction.artifacts import ContextArtifactStore
from mewcode_agent.compaction.models import (
    ArtifactReference,
    CompactionConfig,
    ContextCompactionError,
    ToolCompactionResult,
)
from mewcode_agent.history import ConversationHistory, ToolMessageReplacement
from mewcode_agent.models import ChatMessage


_OMITTED_TEXT = "\n...[externalized content omitted]...\n"


@dataclass(frozen=True, slots=True)
class _ToolBatch:
    assistant_index: int
    tool_indexes: tuple[int, ...]

    @property
    def end_index(self) -> int:
        return self.tool_indexes[-1] + 1


class ToolResultCompactor:
    """Externalize new complete tool batches in deterministic order."""

    def __init__(
        self,
        store: ContextArtifactStore,
        *,
        config: CompactionConfig | None = None,
    ) -> None:
        self._store = store
        self._config = config or CompactionConfig()
        self._processed_batch_ends: set[int] = set()

    async def compact(
        self,
        history: ConversationHistory,
    ) -> ToolCompactionResult:
        snapshot = history.snapshot()
        batches = self._find_batches(snapshot)
        processed = 0
        externalized = 0
        original_total = 0
        compacted_total = 0
        for batch in batches:
            if batch.end_index in self._processed_batch_ends:
                continue
            result = await self._compact_batch(history, snapshot, batch)
            self._processed_batch_ends.add(batch.end_index)
            processed += 1
            externalized += result.externalized_results
            original_total += result.original_inline_bytes
            compacted_total += result.compacted_inline_bytes
            snapshot = history.snapshot()
        return ToolCompactionResult(
            processed,
            externalized,
            original_total,
            compacted_total,
        )

    def reset_session(self) -> None:
        self._processed_batch_ends.clear()

    @staticmethod
    def _find_batches(messages: list[ChatMessage]) -> tuple[_ToolBatch, ...]:
        batches: list[_ToolBatch] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.role != "assistant" or not message.tool_calls:
                index += 1
                continue
            expected_ids = tuple(call.call_id for call in message.tool_calls)
            if len(expected_ids) != len(set(expected_ids)):
                raise ContextCompactionError(
                    "context_invalid_tool_batch",
                    "工具调用批次包含重复 call_id",
                )
            end = index + 1 + len(expected_ids)
            if end > len(messages):
                raise ContextCompactionError(
                    "context_invalid_tool_batch",
                    "工具调用批次缺少结果",
                )
            tool_indexes = tuple(range(index + 1, end))
            actual_ids: list[str] = []
            for tool_index in tool_indexes:
                tool_message = messages[tool_index]
                if tool_message.role != "tool" or tool_message.tool_call_id is None:
                    raise ContextCompactionError(
                        "context_invalid_tool_batch",
                        "工具调用批次包含非 tool 结果",
                    )
                actual_ids.append(tool_message.tool_call_id)
            if tuple(actual_ids) != expected_ids:
                raise ContextCompactionError(
                    "context_invalid_tool_batch",
                    "工具结果顺序或 call_id 不匹配",
                )
            batches.append(_ToolBatch(index, tool_indexes))
            index = end
        return tuple(batches)

    async def _compact_batch(
        self,
        history: ConversationHistory,
        snapshot: list[ChatMessage],
        batch: _ToolBatch,
    ) -> ToolCompactionResult:
        contents = {
            index: snapshot[index].content for index in batch.tool_indexes
        }
        sizes = {
            index: len(content.encode("utf-8"))
            for index, content in contents.items()
        }
        original_total = sum(sizes.values())
        order = sorted(batch.tool_indexes, key=lambda item: (-sizes[item], item))
        references: dict[int, ArtifactReference] = {}
        compacted_contents = dict(contents)

        for index in order:
            if sizes[index] <= self._config.single_tool_result_bytes:
                continue
            reference = await self._store.write(contents[index])
            references[index] = reference
            compacted_contents[index] = self._preview_content(
                contents[index],
                reference,
                metadata_only=False,
            )

        def inline_total() -> int:
            return sum(
                len(compacted_contents[index].encode("utf-8"))
                for index in batch.tool_indexes
            )

        for index in order:
            if inline_total() <= self._config.tool_batch_bytes:
                break
            if index in references:
                continue
            reference = await self._store.write(contents[index])
            references[index] = reference
            compacted_contents[index] = self._preview_content(
                contents[index],
                reference,
                metadata_only=False,
            )

        for index in order:
            if inline_total() <= self._config.tool_batch_bytes:
                break
            reference = references.get(index)
            if reference is None:
                continue
            compacted_contents[index] = self._preview_content(
                contents[index],
                reference,
                metadata_only=True,
            )

        if inline_total() > self._config.tool_batch_bytes:
            raise ContextCompactionError(
                "context_artifact_budget_exceeded",
                "工具结果引用 metadata 超过批次内联上限",
            )

        replacements = tuple(
            ToolMessageReplacement(
                index=index,
                expected_tool_call_id=(
                    snapshot[index].tool_call_id or ""
                ),
                expected_content_sha256=sha256(
                    contents[index].encode("utf-8")
                ).hexdigest(),
                message=ChatMessage(
                    role="tool",
                    content=compacted_contents[index],
                    tool_call_id=snapshot[index].tool_call_id,
                ),
            )
            for index in batch.tool_indexes
            if index in references
        )
        if replacements:
            try:
                history.replace_tool_messages(replacements)
            except ValueError as exc:
                raise ContextCompactionError(
                    "context_invalid_tool_batch",
                    "工具结果历史在压缩期间发生变化",
                ) from exc
        return ToolCompactionResult(
            processed_batches=1,
            externalized_results=len(references),
            original_inline_bytes=original_total,
            compacted_inline_bytes=inline_total(),
        )

    def _preview_content(
        self,
        content: str,
        reference: ArtifactReference,
        *,
        metadata_only: bool,
    ) -> str:
        raw = self._parse_tool_result(content)
        preview = "" if metadata_only else self._preview(content)
        metadata = {
            "path": str(reference.path),
            "sha256": reference.sha256,
            "utf8_bytes": reference.utf8_bytes,
            "preview": preview,
            "preview_truncated": True,
            "reader_tool": "read_context_artifact",
        }
        if raw["success"] is True:
            compacted: dict[str, Any] = {
                "tool_name": raw["tool_name"],
                "success": True,
                "data": {"externalized": metadata},
            }
        else:
            error = raw["error"]
            compacted = {
                "tool_name": raw["tool_name"],
                "success": False,
                "error": {
                    "code": error["code"],
                    "message": error["message"],
                    "details": {"externalized": metadata},
                },
            }
        return json.dumps(
            compacted,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _parse_tool_result(content: str) -> dict[str, Any]:
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ContextCompactionError(
                "context_invalid_tool_batch",
                "tool 历史不是有效 JSON",
            ) from exc
        if not isinstance(raw, dict):
            raise ContextCompactionError(
                "context_invalid_tool_batch",
                "tool 历史根节点不是 object",
            )
        if (
            not isinstance(raw.get("tool_name"), str)
            or type(raw.get("success")) is not bool
        ):
            raise ContextCompactionError(
                "context_invalid_tool_batch",
                "tool 历史字段无效",
            )
        if raw["success"] is False:
            error = raw.get("error")
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), str)
                or not isinstance(error.get("message"), str)
            ):
                raise ContextCompactionError(
                    "context_invalid_tool_batch",
                    "失败 tool 历史字段无效",
                )
        return raw

    def _preview(self, content: str) -> str:
        payload = content.encode("utf-8")
        if len(payload) <= self._config.preview_bytes:
            return content
        head = self._decode_prefix(payload, self._config.preview_head_bytes)
        tail = self._decode_suffix(payload, self._config.preview_tail_bytes)
        return head + _OMITTED_TEXT + tail

    @staticmethod
    def _decode_prefix(payload: bytes, budget: int) -> str:
        selected = payload[:budget]
        while selected:
            try:
                return selected.decode("utf-8")
            except UnicodeDecodeError as exc:
                selected = selected[: exc.start]
        return ""

    @staticmethod
    def _decode_suffix(payload: bytes, budget: int) -> str:
        selected = payload[-budget:]
        while selected:
            try:
                return selected.decode("utf-8")
            except UnicodeDecodeError as exc:
                selected = selected[exc.end :]
        return ""
