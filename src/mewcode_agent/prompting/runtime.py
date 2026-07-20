"""Append-only runtime controls with explicit request/round lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mewcode_agent.prompting.builtins import (
    EXECUTION_MODE_TEXT,
    FINAL_ROUND_TEXT,
    PLANNING_FULL_TEXT,
    PLANNING_REMINDER_TEXT,
)
from mewcode_agent.prompting.environment import (
    RequestEnvironmentCollector,
    SessionEnvironment,
)
from mewcode_agent.prompting.models import ControlMessage, RuntimeInstruction

if TYPE_CHECKING:
    from mewcode_agent.agent.events import AgentRunMode


class PromptRuntime:
    def __init__(
        self,
        session_environment: SessionEnvironment,
        request_environment_collector: RequestEnvironmentCollector,
        *,
        session_controls: tuple[RuntimeInstruction, ...] = (),
    ) -> None:
        self._collector = request_environment_collector
        self._session_environment = session_environment
        self._validate_session_controls(session_controls)
        self._reset_timeline(session_controls)

    @staticmethod
    def _validate_session_controls(
        session_controls: tuple[RuntimeInstruction, ...],
    ) -> None:
        if not isinstance(session_controls, tuple):
            raise ValueError("session_controls 必须是 tuple")
        seen: set[str] = set()
        for instruction in session_controls:
            if not isinstance(instruction, RuntimeInstruction):
                raise ValueError("session_controls 元素类型无效")
            if instruction.scope != "session":
                raise ValueError("session_controls 只接受 scope=session")
            if instruction.instruction_id in seen:
                raise ValueError("session_controls instruction_id 不能重复")
            seen.add(instruction.instruction_id)

    def _reset_timeline(
        self,
        session_controls: tuple[RuntimeInstruction, ...],
    ) -> None:
        self._timeline: list[ControlMessage] = []
        self._dynamic_session_controls: tuple[RuntimeInstruction, ...] = ()
        self._ids: set[str] = set()
        self._sequence = 0
        self._request_counter = 0
        self._active_request: int | None = None
        self._active_round: int | None = None
        self._last_round = 0
        self._round_sealed = False
        self._append(
            RuntimeInstruction(
                "runtime.environment.session",
                "context",
                "session",
                self._session_environment.to_json(),
                "environment",
            ),
            anchor=0,
        )
        for instruction in session_controls:
            self._append(instruction, anchor=0)

    def reset_session(
        self,
        *,
        session_controls: tuple[RuntimeInstruction, ...] = (),
    ) -> None:
        if self._active_request is not None or self._active_round is not None:
            raise RuntimeError("活动 request 或 round 期间不能重置 session")
        self._validate_session_controls(session_controls)
        self._reset_timeline(session_controls)

    def replace_dynamic_session_controls(
        self,
        controls: tuple[RuntimeInstruction, ...],
    ) -> None:
        """Replace runtime-owned session controls without touching history."""

        self._validate_session_controls(controls)
        static_ids = {message.instruction_id for message in self._timeline}
        conflict = next(
            (
                control.instruction_id
                for control in controls
                if control.instruction_id in static_ids
            ),
            None,
        )
        if conflict is not None:
            raise ValueError(f"dynamic session control id 冲突: {conflict}")
        self._dynamic_session_controls = controls

    @staticmethod
    def _history_length(value: int) -> int:
        if type(value) is not int or value < 0:
            raise ValueError(
                "history_length 必须为大于或等于 0 的整数"
            )
        return value

    def _append(
        self,
        instruction: RuntimeInstruction,
        *,
        anchor: int,
    ) -> ControlMessage:
        anchor = self._history_length(anchor)
        if self._timeline and anchor < self._timeline[-1].anchor:
            raise ValueError("控制消息 anchor 不能回退")
        if instruction.instruction_id in self._ids:
            raise ValueError("instruction_id 不能重复")
        request_sequence = (
            self._active_request
            if instruction.scope in ("request", "round")
            else None
        )
        round_number = (
            self._active_round if instruction.scope == "round" else None
        )
        self._sequence += 1
        message = ControlMessage(
            instruction.instruction_id,
            instruction.kind,
            instruction.scope,
            instruction.content,
            self._sequence,
            anchor,
            request_sequence,
            round_number,
        )
        self._timeline.append(message)
        self._ids.add(instruction.instruction_id)
        return message

    async def begin_request(
        self,
        *,
        history_length: int,
        mode: AgentRunMode,
    ) -> int:
        anchor = self._history_length(history_length)
        if mode not in ("planning", "executing"):
            raise ValueError("mode 必须为 planning 或 executing")
        if self._active_request is not None:
            raise RuntimeError("已有活动 request")
        if self._timeline and anchor < self._timeline[-1].anchor:
            raise ValueError("控制消息 anchor 不能回退")
        environment = await self._collector.collect()
        self._request_counter += 1
        self._active_request = self._request_counter
        self._last_round = 0
        request_id = self._active_request
        self._append(
            RuntimeInstruction(
                f"runtime.environment.request_{request_id}",
                "context",
                "request",
                environment.to_json(),
                "environment",
            ),
            anchor=anchor,
        )
        if mode == "executing":
            self._append(
                RuntimeInstruction(
                    f"runtime.mode.execution.request_{request_id}",
                    "instruction",
                    "request",
                    EXECUTION_MODE_TEXT,
                    "builtin",
                ),
                anchor=anchor,
            )
        return request_id

    def begin_round(
        self,
        *,
        history_length: int,
        round_number: int,
        max_rounds: int,
        mode: AgentRunMode,
    ) -> None:
        anchor = self._history_length(history_length)
        if mode not in ("planning", "executing"):
            raise ValueError("mode 必须为 planning 或 executing")
        if self._active_request is None:
            raise RuntimeError("没有活动 request")
        if self._active_round is not None:
            raise RuntimeError("已有活动 round")
        if type(round_number) is not int or round_number != self._last_round + 1:
            raise ValueError(
                "round_number 必须在当前 request 内从 1 连续递增"
            )
        if (
            type(max_rounds) is not int
            or max_rounds <= 0
            or round_number > max_rounds
        ):
            raise ValueError("max_rounds 与 round_number 不一致")
        if self._timeline and anchor < self._timeline[-1].anchor:
            raise ValueError("控制消息 anchor 不能回退")
        self._active_round = round_number
        self._round_sealed = False
        request_id = self._active_request
        self._append(
            RuntimeInstruction(
                f"runtime.state.request_{request_id}.round_{round_number}",
                "state",
                "round",
                (
                    f"当前运行状态：request={request_id}，"
                    f"round={round_number}/{max_rounds}，mode={mode}。"
                ),
                "runtime",
            ),
            anchor=anchor,
        )
        if mode == "planning":
            full = round_number in (1, 6, 11)
            label = "planning_full" if full else "planning_reminder"
            self._append(
                RuntimeInstruction(
                    (
                        f"runtime.mode.{label}."
                        f"request_{request_id}.round_{round_number}"
                    ),
                    "instruction",
                    "round",
                    PLANNING_FULL_TEXT if full else PLANNING_REMINDER_TEXT,
                    "builtin",
                ),
                anchor=anchor,
            )
        if round_number == max_rounds:
            self._append(
                RuntimeInstruction(
                    (
                        "runtime.limit.final_round."
                        f"request_{request_id}.round_{round_number}"
                    ),
                    "instruction",
                    "round",
                    FINAL_ROUND_TEXT,
                    "builtin",
                ),
                anchor=anchor,
            )

    def inject(
        self,
        instruction: RuntimeInstruction,
        *,
        history_length: int,
    ) -> ControlMessage:
        if instruction.kind == "state":
            raise ValueError("kind=state 只能由 begin_round 创建")
        if instruction.scope == "request" and self._active_request is None:
            raise RuntimeError("没有活动 request")
        if instruction.scope == "round":
            if self._active_round is None:
                raise RuntimeError("没有活动 round")
            if self._round_sealed:
                raise RuntimeError("当前 round 已 seal")
        return self._append(instruction, anchor=history_length)

    def seal_round(self) -> None:
        if self._active_round is None:
            raise RuntimeError("没有活动 round")
        if self._round_sealed:
            raise RuntimeError("当前 round 已 seal")
        self._round_sealed = True

    def end_round(self) -> None:
        if self._active_round is None:
            raise RuntimeError("没有活动 round")
        self._last_round = self._active_round
        self._active_round = None
        self._round_sealed = False

    def end_request(self) -> None:
        if self._active_request is None:
            raise RuntimeError("没有活动 request")
        if self._active_round is not None:
            raise RuntimeError("活动 round 结束前不能结束 request")
        self._active_request = None
        self._last_round = 0

    def timeline(self) -> tuple[ControlMessage, ...]:
        if not self._dynamic_session_controls:
            return tuple(self._timeline)
        base_end = 0
        for message in self._timeline:
            if message.scope != "session" or message.anchor != 0:
                break
            base_end += 1
        combined: list[ControlMessage] = list(self._timeline[:base_end])
        for instruction in self._dynamic_session_controls:
            combined.append(
                ControlMessage(
                    instruction.instruction_id,
                    instruction.kind,
                    instruction.scope,
                    instruction.content,
                    1,
                    0,
                    None,
                    None,
                )
            )
        combined.extend(self._timeline[base_end:])
        return tuple(
            ControlMessage(
                message.instruction_id,
                message.kind,
                message.scope,
                message.content,
                sequence,
                message.anchor,
                message.request_sequence,
                message.round_number,
            )
            for sequence, message in enumerate(combined, start=1)
        )

    @property
    def active_request_sequence(self) -> int | None:
        return self._active_request

    @property
    def active_round_number(self) -> int | None:
        return self._active_round
