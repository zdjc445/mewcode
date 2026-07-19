from dataclasses import FrozenInstanceError

import pytest

from mewcode_agent.models import ChatMessage
from mewcode_agent.prompting.models import (
    ControlMessage,
    PromptFrame,
    PromptModule,
    RuntimeInstruction,
)


def test_prompt_module_is_frozen_and_validates_exact_identifier() -> None:
    module = PromptModule(
        module_id="coding.project_rules",
        priority=500,
        content="规则",
        source="project",
        protected=False,
    )

    with pytest.raises(FrozenInstanceError):
        module.content = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="module_id"):
        PromptModule("Coding.Rules", 500, "规则", "project", False)


@pytest.mark.parametrize("priority", [True, 1.5])
def test_prompt_module_rejects_invalid_priority(priority: object) -> None:
    with pytest.raises(ValueError, match="priority"):
        PromptModule(
            "coding.rules",
            priority,  # type: ignore[arg-type]
            "规则",
            "project",
            False,
        )


def test_external_prompt_module_cannot_be_protected() -> None:
    with pytest.raises(ValueError, match="protected"):
        PromptModule("coding.rules", 500, "规则", "user", True)


@pytest.mark.parametrize(
    ("scope", "request_sequence", "round_number"),
    [
        ("session", 1, None),
        ("request", None, None),
        ("request", 1, 2),
        ("round", 1, None),
    ],
)
def test_control_message_rejects_scope_target_mismatch(
    scope: str,
    request_sequence: int | None,
    round_number: int | None,
) -> None:
    with pytest.raises(ValueError, match="scope"):
        ControlMessage(
            instruction_id="runtime.test",
            kind="instruction",
            scope=scope,  # type: ignore[arg-type]
            content="规则",
            sequence=1,
            anchor=0,
            request_sequence=request_sequence,
            round_number=round_number,
        )


def test_state_control_requires_round_scope() -> None:
    with pytest.raises(ValueError, match="state"):
        RuntimeInstruction(
            instruction_id="runtime.state",
            kind="state",
            scope="request",
            content="状态",
            source="test",
        )


def test_prompt_frame_accepts_chat_and_control_items() -> None:
    control = ControlMessage(
        instruction_id="runtime.environment.session",
        kind="context",
        scope="session",
        content='{"shell":"powershell.exe"}',
        sequence=1,
        anchor=0,
        request_sequence=None,
        round_number=None,
    )
    user = ChatMessage(role="user", content="任务")

    frame = PromptFrame("system", (control, user))

    assert frame.items == (control, user)
