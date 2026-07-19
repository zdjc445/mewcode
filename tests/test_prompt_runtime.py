from __future__ import annotations

import pytest

from mewcode_agent.prompting.environment import (
    GitEnvironment,
    RequestEnvironment,
    SessionEnvironment,
)
from mewcode_agent.prompting.models import RuntimeInstruction
from mewcode_agent.prompting.runtime import PromptRuntime


class FixedRequestEnvironmentCollector:
    async def collect(self) -> RequestEnvironment:
        return RequestEnvironment(
            "2026-07-18T12:00:00+08:00",
            GitEnvironment("repository", "master", "", None),
        )


def make_runtime() -> PromptRuntime:
    return PromptRuntime(
        SessionEnvironment(
            "Windows",
            "powershell.exe",
            "D:\\workspace",
            "China Standard Time",
            "+08:00",
        ),
        FixedRequestEnvironmentCollector(),
    )


@pytest.mark.asyncio
async def test_execution_request_and_round_use_fixed_order() -> None:
    runtime = make_runtime()

    request_sequence = await runtime.begin_request(
        history_length=2,
        mode="executing",
    )
    runtime.begin_round(
        history_length=3,
        round_number=1,
        max_rounds=15,
        mode="executing",
    )
    runtime.seal_round()

    timeline = runtime.timeline()
    assert request_sequence == 1
    assert [item.kind for item in timeline] == [
        "context",
        "context",
        "instruction",
        "state",
    ]
    assert [item.anchor for item in timeline] == [0, 2, 2, 3]
    assert [item.sequence for item in timeline] == [1, 2, 3, 4]
    assert timeline[-1].content == (
        "当前运行状态：request=1，round=1/15，mode=executing。"
    )


@pytest.mark.asyncio
async def test_planning_full_rule_repeats_on_rounds_1_6_11() -> None:
    runtime = make_runtime()
    await runtime.begin_request(history_length=0, mode="planning")

    instruction_ids: list[str] = []
    for round_number in range(1, 16):
        runtime.begin_round(
            history_length=round_number,
            round_number=round_number,
            max_rounds=15,
            mode="planning",
        )
        runtime.seal_round()
        instruction_ids.extend(
            item.instruction_id
            for item in runtime.timeline()
            if item.round_number == round_number
            and item.kind == "instruction"
        )
        runtime.end_round()

    full = [item for item in instruction_ids if ".planning_full." in item]
    reminder = [
        item for item in instruction_ids if ".planning_reminder." in item
    ]
    final = [item for item in instruction_ids if ".final_round." in item]
    assert len(full) == 3
    assert len(reminder) == 12
    assert len(final) == 1
    assert runtime.timeline()[0].instruction_id == (
        "runtime.environment.session"
    )


@pytest.mark.asyncio
async def test_scope_end_keeps_archived_controls_but_clears_active_state() -> None:
    runtime = make_runtime()
    await runtime.begin_request(history_length=0, mode="planning")
    runtime.begin_round(
        history_length=1,
        round_number=1,
        max_rounds=15,
        mode="planning",
    )
    runtime.seal_round()
    before = runtime.timeline()
    runtime.end_round()
    runtime.end_request()

    assert runtime.timeline() == before
    with pytest.raises(RuntimeError, match="活动 request"):
        runtime.inject(
            RuntimeInstruction(
                "runtime.after_request",
                "instruction",
                "request",
                "规则",
                "test",
            ),
            history_length=1,
        )


@pytest.mark.asyncio
async def test_runtime_rejects_duplicate_request_and_round_lifecycle() -> None:
    runtime = make_runtime()
    with pytest.raises(ValueError, match="mode"):
        await runtime.begin_request(
            history_length=0,
            mode="invalid",  # type: ignore[arg-type]
        )
    with pytest.raises(RuntimeError, match="没有活动 request"):
        runtime.begin_round(
            history_length=0,
            round_number=1,
            max_rounds=15,
            mode="executing",
        )
    await runtime.begin_request(history_length=0, mode="executing")
    with pytest.raises(RuntimeError, match="已有活动 request"):
        await runtime.begin_request(history_length=0, mode="executing")
    runtime.begin_round(
        history_length=1,
        round_number=1,
        max_rounds=15,
        mode="executing",
    )
    with pytest.raises(RuntimeError, match="已有活动 round"):
        runtime.begin_round(
            history_length=1,
            round_number=2,
            max_rounds=15,
            mode="executing",
        )
    with pytest.raises(RuntimeError, match="活动 round"):
        runtime.end_request()


@pytest.mark.asyncio
async def test_round_number_must_be_contiguous() -> None:
    runtime = make_runtime()
    await runtime.begin_request(history_length=0, mode="planning")

    with pytest.raises(ValueError, match="连续递增"):
        runtime.begin_round(
            history_length=1,
            round_number=2,
            max_rounds=15,
            mode="planning",
        )


@pytest.mark.asyncio
async def test_sealed_round_rejects_round_injection_and_state_is_reserved() -> None:
    runtime = make_runtime()
    await runtime.begin_request(history_length=0, mode="planning")
    runtime.begin_round(
        history_length=1,
        round_number=1,
        max_rounds=15,
        mode="planning",
    )
    state = RuntimeInstruction(
        "runtime.external_state",
        "state",
        "round",
        "状态",
        "test",
    )
    with pytest.raises(ValueError, match="begin_round"):
        runtime.inject(state, history_length=1)
    runtime.seal_round()
    with pytest.raises(RuntimeError, match="已 seal"):
        runtime.inject(
            RuntimeInstruction(
                "runtime.late_round",
                "instruction",
                "round",
                "规则",
                "test",
            ),
            history_length=1,
        )


@pytest.mark.asyncio
async def test_runtime_rejects_duplicate_id_negative_and_regressing_anchor() -> None:
    runtime = make_runtime()
    with pytest.raises(ValueError, match="history_length"):
        await runtime.begin_request(history_length=-1, mode="executing")
    await runtime.begin_request(history_length=2, mode="executing")
    with pytest.raises(ValueError, match="重复"):
        runtime.inject(
            RuntimeInstruction(
                "runtime.environment.session",
                "context",
                "session",
                "duplicate",
                "test",
            ),
            history_length=2,
        )
    with pytest.raises(ValueError, match="anchor"):
        runtime.inject(
            RuntimeInstruction(
                "runtime.anchor_regression",
                "context",
                "session",
                "context",
                "test",
            ),
            history_length=1,
        )
