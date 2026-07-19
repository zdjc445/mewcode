"""One-run approval and cancellation context for the agent loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from mewcode_agent.agent.events import (
    PlanApprovalDecision,
    ToolApprovalDecision,
)


@dataclass(frozen=True, slots=True)
class PlanApprovalResolution:
    decision: PlanApprovalDecision
    feedback: str = ""


class AgentRunCancelled(Exception):
    """Internal signal raised when cancellation wins an approval wait."""


class AgentRunContext:
    """Carry reverse-channel decisions for exactly one AgentLoop.run()."""

    def __init__(self) -> None:
        self._used = False
        self._active = False
        self._cancelled = asyncio.Event()
        self._tool_approvals: dict[
            str,
            asyncio.Future[ToolApprovalDecision],
        ] = {}
        self._plan_approvals: dict[
            str,
            asyncio.Future[PlanApprovalResolution],
        ] = {}

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def begin_run(self) -> None:
        if self._used:
            raise ValueError("AgentRunContext 只能服务一次 run()")
        self._used = True
        self._active = True

    def finish_run(self) -> None:
        self._active = False
        futures = (
            *self._tool_approvals.values(),
            *self._plan_approvals.values(),
        )
        for future in futures:
            future.cancel()
        self._tool_approvals.clear()
        self._plan_approvals.clear()

    def cancel(self) -> None:
        self._cancelled.set()

    async def wait_cancelled(self) -> None:
        await self._cancelled.wait()

    def open_tool_approval(self) -> str:
        self._require_active()
        request_id = uuid4().hex
        self._tool_approvals[request_id] = (
            asyncio.get_running_loop().create_future()
        )
        return request_id

    def open_plan_approval(self) -> str:
        self._require_active()
        request_id = uuid4().hex
        self._plan_approvals[request_id] = (
            asyncio.get_running_loop().create_future()
        )
        return request_id

    def resolve_tool_approval(
        self,
        request_id: str,
        decision: ToolApprovalDecision,
    ) -> None:
        if decision not in (
            "allow_once",
            "allow_session",
            "allow_permanent",
            "reject",
        ):
            raise ValueError("不支持的工具审批选择")
        future = self._pending_future(self._tool_approvals, request_id)
        future.set_result(decision)

    def resolve_plan_approval(
        self,
        request_id: str,
        decision: PlanApprovalDecision,
        *,
        feedback: str = "",
    ) -> None:
        if decision not in (
            "execute_current",
            "request_changes",
            "reject",
        ):
            raise ValueError("不支持的计划审批选择")
        if decision == "request_changes":
            if not feedback.strip():
                raise ValueError("request_changes 的 feedback 必须非空")
        elif feedback:
            raise ValueError("只有 request_changes 可以携带 feedback")
        future = self._pending_future(self._plan_approvals, request_id)
        future.set_result(PlanApprovalResolution(decision, feedback))

    async def wait_for_tool_approval(
        self,
        request_id: str,
    ) -> ToolApprovalDecision:
        future = self._pending_future(
            self._tool_approvals,
            request_id,
            allow_done=True,
        )
        return await self._wait_for_decision(
            request_id,
            future,
            self._tool_approvals,
        )

    async def wait_for_plan_approval(
        self,
        request_id: str,
    ) -> PlanApprovalResolution:
        future = self._pending_future(
            self._plan_approvals,
            request_id,
            allow_done=True,
        )
        return await self._wait_for_decision(
            request_id,
            future,
            self._plan_approvals,
        )

    def _require_active(self) -> None:
        if not self._active:
            raise ValueError("AgentRunContext 未处于运行状态")

    @staticmethod
    def _pending_future(
        pending: dict[str, asyncio.Future[object]],
        request_id: str,
        *,
        allow_done: bool = False,
    ) -> asyncio.Future[object]:
        future = pending.get(request_id)
        if (
            not request_id
            or future is None
            or (future.done() and not allow_done)
        ):
            raise ValueError("审批 request_id 未知、过期或已完成")
        return future

    async def _wait_for_decision(
        self,
        request_id: str,
        future: asyncio.Future[object],
        pending: dict[str, asyncio.Future[object]],
    ) -> object:
        cancel_task = asyncio.create_task(self.wait_cancelled())
        try:
            done, _ = await asyncio.wait(
                {future, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                raise AgentRunCancelled
            return future.result()
        finally:
            cancel_task.cancel()
            pending.pop(request_id, None)
