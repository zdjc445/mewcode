"""Pure prompt assembly and shared control-message rendering."""

from html import escape

from mewcode_agent.models import ChatMessage
from mewcode_agent.prompting.models import (
    ControlMessage,
    PromptFrame,
    PromptModule,
)


def render_control_message(message: ControlMessage) -> str:
    attributes = [
        ("kind", message.kind),
        ("scope", message.scope),
        ("sequence", str(message.sequence)),
    ]
    if message.request_sequence is not None:
        attributes.append(("request", str(message.request_sequence)))
    if message.round_number is not None:
        attributes.append(("round", str(message.round_number)))
    opening = "<mewcode-control\n" + "\n".join(
        f'  {name}="{escape(value, quote=True)}"'
        for name, value in attributes
    ) + ">"
    return (
        f"{opening}\n"
        f"{escape(message.content, quote=False)}\n"
        "</mewcode-control>"
    )


class PromptComposer:
    def __init__(self, modules: tuple[PromptModule, ...]) -> None:
        if not modules:
            raise ValueError("Prompt 模块目录不能为空")
        self._modules = tuple(
            sorted(
                modules,
                key=lambda item: (item.priority, item.module_id),
            )
        )

    def compose(
        self,
        history: list[ChatMessage],
        timeline: tuple[ControlMessage, ...],
    ) -> PromptFrame:
        last_sequence = 0
        last_anchor = 0
        for message in timeline:
            if message.sequence <= last_sequence:
                raise ValueError("控制消息 sequence 必须严格递增")
            if message.anchor < last_anchor:
                raise ValueError("控制消息 anchor 不能回退")
            if message.anchor > len(history):
                raise ValueError("控制消息 anchor 超出普通历史")
            last_sequence = message.sequence
            last_anchor = message.anchor

        controls_by_anchor: dict[int, list[ControlMessage]] = {}
        for message in timeline:
            controls_by_anchor.setdefault(message.anchor, []).append(message)
        items: list[ChatMessage | ControlMessage] = []
        for anchor in range(len(history) + 1):
            items.extend(controls_by_anchor.get(anchor, ()))
            if anchor < len(history):
                items.append(history[anchor])
        system_prompt = "\n\n".join(
            f"## {module.module_id}\n{module.content}"
            for module in self._modules
        )
        return PromptFrame(system_prompt, tuple(items))
