import asyncio

import pytest

from mewcode_agent.agent.context import AgentRunCancelled, AgentRunContext


@pytest.mark.asyncio
async def test_tool_approval_is_resolved_exactly_once() -> None:
    context = AgentRunContext()
    context.begin_run()
    request_id = context.open_tool_approval()
    waiter = asyncio.create_task(context.wait_for_tool_approval(request_id))

    context.resolve_tool_approval(request_id, "allow_once")

    assert await waiter == "allow_once"
    with pytest.raises(ValueError, match="未知、过期或已完成"):
        context.resolve_tool_approval(request_id, "reject")


@pytest.mark.asyncio
async def test_plan_changes_require_non_blank_feedback() -> None:
    context = AgentRunContext()
    context.begin_run()
    request_id = context.open_plan_approval()

    with pytest.raises(ValueError, match="feedback"):
        context.resolve_plan_approval(
            request_id,
            "request_changes",
            feedback=" ",
        )

    context.resolve_plan_approval(
        request_id,
        "request_changes",
        feedback="补充回滚步骤",
    )
    resolution = await context.wait_for_plan_approval(request_id)

    assert resolution.decision == "request_changes"
    assert resolution.feedback == "补充回滚步骤"


@pytest.mark.asyncio
async def test_non_change_plan_decision_rejects_feedback() -> None:
    context = AgentRunContext()
    context.begin_run()
    request_id = context.open_plan_approval()

    with pytest.raises(ValueError, match="feedback"):
        context.resolve_plan_approval(
            request_id,
            "execute_current",
            feedback="不允许",
        )


@pytest.mark.asyncio
async def test_cancel_interrupts_approval_wait_and_is_idempotent() -> None:
    context = AgentRunContext()
    context.begin_run()
    request_id = context.open_tool_approval()
    waiter = asyncio.create_task(context.wait_for_tool_approval(request_id))

    context.cancel()
    context.cancel()

    with pytest.raises(AgentRunCancelled):
        await waiter


@pytest.mark.asyncio
async def test_cancel_waiter_completes_after_cancel() -> None:
    context = AgentRunContext()
    context.begin_run()
    waiter = asyncio.create_task(context.wait_cancelled())

    context.cancel()

    await waiter
    assert context.cancelled is True


@pytest.mark.parametrize(
    ("decision", "feedback"),
    [
        ("execute_current", ""),
        ("request_changes", "修改计划"),
        ("reject", ""),
    ],
)
@pytest.mark.asyncio
async def test_plan_approval_accepts_each_valid_decision(
    decision: str,
    feedback: str,
) -> None:
    context = AgentRunContext()
    context.begin_run()
    request_id = context.open_plan_approval()

    context.resolve_plan_approval(
        request_id,
        decision,  # type: ignore[arg-type]
        feedback=feedback,
    )

    resolution = await context.wait_for_plan_approval(request_id)
    assert resolution.decision == decision
    assert resolution.feedback == feedback


def test_unknown_approval_id_is_rejected() -> None:
    context = AgentRunContext()
    context.begin_run()

    with pytest.raises(ValueError, match="未知、过期或已完成"):
        context.resolve_tool_approval("missing", "reject")
    with pytest.raises(ValueError, match="未知、过期或已完成"):
        context.resolve_plan_approval("missing", "reject")


def test_context_can_begin_only_one_run() -> None:
    context = AgentRunContext()
    context.begin_run()
    context.finish_run()

    with pytest.raises(ValueError, match="只能服务一次"):
        context.begin_run()


def test_approval_request_requires_active_run() -> None:
    context = AgentRunContext()

    with pytest.raises(ValueError, match="未处于运行状态"):
        context.open_tool_approval()
