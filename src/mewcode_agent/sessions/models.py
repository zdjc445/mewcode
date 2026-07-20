"""Validated data contracts and safe errors for persisted sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Literal, Mapping, TypeAlias, cast

from mewcode_agent.models import ChatMessage, ThinkingBlock, ToolCall

SESSION_SCHEMA_VERSION = 1
SESSION_RECORD_BYTES = 65 * 1024 * 1024
SESSION_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")

SessionErrorCode: TypeAlias = Literal[
    "session_write_failed",
    "session_record_too_large",
    "session_not_found",
    "session_invalid_meta",
    "session_repair_failed",
    "session_access_denied",
    "session_delete_active",
    "session_delete_failed",
    "session_resume_failed",
]
SessionDiagnosticCode: TypeAlias = Literal[
    "session_line_too_large",
    "session_line_missing_newline",
    "session_line_invalid_newline",
    "session_line_invalid_utf8",
    "session_line_invalid_json",
    "session_line_invalid_schema",
    "session_line_sequence_not_increasing",
    "session_tool_batch_invalid",
]
SessionCommandKind: TypeAlias = Literal[
    "list",
    "resume",
    "path",
    "delete",
]

_ERROR_MESSAGES: dict[SessionErrorCode, str] = {
    "session_write_failed": "会话消息或元数据写入失败",
    "session_record_too_large": "会话消息记录超过 65 MiB",
    "session_not_found": "会话不存在",
    "session_invalid_meta": "会话元数据无效且无法安全重建",
    "session_repair_failed": "会话异常恢复重写失败",
    "session_access_denied": "会话路径不属于会话根目录",
    "session_delete_active": "不能删除当前活动会话",
    "session_delete_failed": "会话删除失败",
    "session_resume_failed": "会话恢复或运行时重置失败",
}

_RECORD_KEYS = (
    "schema_version",
    "session_id",
    "sequence",
    "created_at",
    "record_type",
    "message",
)
_MESSAGE_KEYS = (
    "role",
    "content",
    "tool_calls",
    "tool_call_id",
    "thinking_blocks",
)
_TOOL_CALL_KEYS = ("call_id", "name", "arguments_json")
_THINKING_KEYS = ("text", "signature")
_META_KEYS = (
    "schema_version",
    "session_id",
    "project_root",
    "provider_id",
    "model",
    "title",
    "summary",
    "message_count",
    "last_sequence",
    "created_at",
    "updated_at",
)


class SessionError(RuntimeError):
    """A stable session failure safe to display in the UI."""

    def __init__(self, code: SessionErrorCode) -> None:
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        super().__init__(f"{code}: {self.message}")


def validate_session_id(value: str) -> None:
    if not isinstance(value, str) or SESSION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("session_id 必须是 32 位小写十六进制字符串")


def parse_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} 必须是包含 UTC offset 的 ISO-8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} 必须是包含 UTC offset 的 ISO-8601 字符串"
        ) from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含 UTC offset")
    return value


def _exact_mapping(
    value: object,
    keys: tuple[str, ...],
    field_name: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != keys:
        raise ValueError(f"{field_name} 字段或字段顺序无效")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} 字段名必须是字符串")
    return cast(Mapping[str, object], value)


def chat_message_to_dict(message: ChatMessage) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [
            {
                "call_id": call.call_id,
                "name": call.name,
                "arguments_json": call.arguments_json,
            }
            for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
        "thinking_blocks": [
            {"text": block.text, "signature": block.signature}
            for block in message.thinking_blocks
        ],
    }


def chat_message_from_dict(value: object) -> ChatMessage:
    data = _exact_mapping(value, _MESSAGE_KEYS, "message")
    role = data["role"]
    content = data["content"]
    tool_call_id = data["tool_call_id"]
    if role not in ("user", "assistant", "tool"):
        raise ValueError("message.role 无效")
    if not isinstance(content, str):
        raise ValueError("message.content 必须是字符串")
    if tool_call_id is not None and not isinstance(tool_call_id, str):
        raise ValueError("message.tool_call_id 必须是字符串或 null")

    raw_calls = data["tool_calls"]
    if not isinstance(raw_calls, list):
        raise ValueError("message.tool_calls 必须是列表")
    calls: list[ToolCall] = []
    for index, raw_call in enumerate(raw_calls):
        call = _exact_mapping(
            raw_call,
            _TOOL_CALL_KEYS,
            f"message.tool_calls[{index}]",
        )
        if not all(
            isinstance(call[key], str) for key in _TOOL_CALL_KEYS
        ):
            raise ValueError("tool call 字段必须是字符串")
        calls.append(
            ToolCall(
                cast(str, call["call_id"]),
                cast(str, call["name"]),
                cast(str, call["arguments_json"]),
            )
        )

    raw_blocks = data["thinking_blocks"]
    if not isinstance(raw_blocks, list):
        raise ValueError("message.thinking_blocks 必须是列表")
    blocks: list[ThinkingBlock] = []
    for index, raw_block in enumerate(raw_blocks):
        block = _exact_mapping(
            raw_block,
            _THINKING_KEYS,
            f"message.thinking_blocks[{index}]",
        )
        if not isinstance(block["text"], str) or not isinstance(
            block["signature"], str
        ):
            raise ValueError("thinking block 字段必须是字符串")
        blocks.append(
            ThinkingBlock(
                cast(str, block["text"]),
                cast(str, block["signature"]),
            )
        )
    return ChatMessage(
        role=role,
        content=content,
        tool_calls=tuple(calls),
        tool_call_id=tool_call_id,
        thinking_blocks=tuple(blocks),
    )


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    sequence: int
    created_at: str
    message: ChatMessage

    def __post_init__(self) -> None:
        validate_session_id(self.session_id)
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("sequence 必须是大于 0 的整数")
        parse_timestamp(self.created_at, "created_at")
        if not isinstance(self.message, ChatMessage):
            raise ValueError("message 类型无效")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "record_type": "message",
            "message": chat_message_to_dict(self.message),
        }

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        expected_session_id: str,
    ) -> SessionRecord:
        data = _exact_mapping(value, _RECORD_KEYS, "record")
        if data["schema_version"] != SESSION_SCHEMA_VERSION or type(
            data["schema_version"]
        ) is not int:
            raise ValueError("record.schema_version 无效")
        if data["record_type"] != "message":
            raise ValueError("record.record_type 无效")
        if data["session_id"] != expected_session_id:
            raise ValueError("record.session_id 不匹配")
        sequence = data["sequence"]
        if type(sequence) is not int or sequence <= 0:
            raise ValueError("record.sequence 无效")
        return cls(
            expected_session_id,
            sequence,
            parse_timestamp(data["created_at"], "record.created_at"),
            chat_message_from_dict(data["message"]),
        )


@dataclass(frozen=True, slots=True)
class SessionMeta:
    session_id: str
    project_root: str
    provider_id: str
    model: str
    title: str
    summary: str
    message_count: int
    last_sequence: int
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        validate_session_id(self.session_id)
        if not isinstance(self.project_root, str):
            raise ValueError("project_root 必须是规范化绝对路径")
        project_path = Path(self.project_root)
        if (
            not project_path.is_absolute()
            or str(project_path) != self.project_root
        ):
            raise ValueError("project_root 必须是规范化绝对路径")
        if not isinstance(self.provider_id, str) or not self.provider_id:
            raise ValueError("provider_id 必须是非空字符串")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model 必须是非空字符串")
        if not isinstance(self.title, str) or not self.title:
            raise ValueError("title 必须是非空字符串")
        if not isinstance(self.summary, str):
            raise ValueError("summary 必须是字符串")
        if type(self.message_count) is not int or self.message_count < 0:
            raise ValueError("message_count 必须是非负整数")
        if type(self.last_sequence) is not int or self.last_sequence < 0:
            raise ValueError("last_sequence 必须是非负整数")
        parse_timestamp(self.created_at, "created_at")
        parse_timestamp(self.updated_at, "updated_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": self.session_id,
            "project_root": self.project_root,
            "provider_id": self.provider_id,
            "model": self.model,
            "title": self.title,
            "summary": self.summary,
            "message_count": self.message_count,
            "last_sequence": self.last_sequence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> SessionMeta:
        data = _exact_mapping(value, _META_KEYS, "meta")
        if data["schema_version"] != SESSION_SCHEMA_VERSION or type(
            data["schema_version"]
        ) is not int:
            raise ValueError("meta.schema_version 无效")
        return cls(
            session_id=cast(str, data["session_id"]),
            project_root=cast(str, data["project_root"]),
            provider_id=cast(str, data["provider_id"]),
            model=cast(str, data["model"]),
            title=cast(str, data["title"]),
            summary=cast(str, data["summary"]),
            message_count=cast(int, data["message_count"]),
            last_sequence=cast(int, data["last_sequence"]),
            created_at=cast(str, data["created_at"]),
            updated_at=cast(str, data["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class SessionDiagnostic:
    line_number: int
    code: SessionDiagnosticCode

    def __post_init__(self) -> None:
        if type(self.line_number) is not int or self.line_number <= 0:
            raise ValueError("line_number 必须是大于 0 的整数")
        if self.code not in (
            "session_line_too_large",
            "session_line_missing_newline",
            "session_line_invalid_newline",
            "session_line_invalid_utf8",
            "session_line_invalid_json",
            "session_line_invalid_schema",
            "session_line_sequence_not_increasing",
            "session_tool_batch_invalid",
        ):
            raise ValueError("diagnostic code 无效")


@dataclass(frozen=True, slots=True)
class SessionRecovery:
    messages: tuple[ChatMessage, ...]
    meta: SessionMeta
    diagnostics: tuple[SessionDiagnostic, ...]
    repaired: bool

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple) or any(
            not isinstance(message, ChatMessage) for message in self.messages
        ):
            raise ValueError("messages 必须是 ChatMessage tuple")
        if self.meta.message_count != len(self.messages):
            raise ValueError("meta.message_count 与 messages 不一致")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, SessionDiagnostic)
            for item in self.diagnostics
        ):
            raise ValueError("diagnostics 必须是 SessionDiagnostic tuple")
        if type(self.repaired) is not bool:
            raise ValueError("repaired 必须是布尔值")


@dataclass(frozen=True, slots=True)
class SessionCommand:
    kind: SessionCommandKind
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("list", "resume", "path", "delete"):
            raise ValueError("session command kind 无效")
        if self.kind == "list":
            if self.session_id is not None:
                raise ValueError("list command 不能包含 session_id")
        else:
            validate_session_id(self.session_id)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class SessionDeleteTarget:
    session_id: str
    title: str
    path: Path

    def __post_init__(self) -> None:
        validate_session_id(self.session_id)
        if not isinstance(self.title, str) or not self.title:
            raise ValueError("title 必须是非空字符串")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("path 必须是绝对 Path")
