import pytest

from mewcode_agent.models import ChatMessage
from mewcode_agent.prompting.composer import (
    PromptComposer,
    render_control_message,
)
from mewcode_agent.prompting.models import ControlMessage, PromptModule
from mewcode_agent.prompting.runtime import PromptRuntime


def control(
    *,
    sequence: int,
    anchor: int,
    content: str = "规则",
) -> ControlMessage:
    return ControlMessage(
        "runtime.test_" + str(sequence),
        "instruction",
        "round",
        content,
        sequence,
        anchor,
        2,
        6,
    )


def test_composer_sorts_static_modules_and_interleaves_by_anchor() -> None:
    modules = (
        PromptModule("project.zeta", 200, "Z", "project", False),
        PromptModule("core.identity", 100, "I", "builtin", True),
        PromptModule("project.alpha", 200, "A", "project", False),
    )
    history = [
        ChatMessage(role="user", content="one"),
        ChatMessage(role="assistant", content="two"),
    ]
    timeline = (
        control(sequence=1, anchor=0),
        control(sequence=2, anchor=2),
    )
    composer = PromptComposer(modules)

    frame = composer.compose(history, timeline)

    assert frame.system_prompt == (
        "## core.identity\nI\n\n"
        "## project.alpha\nA\n\n"
        "## project.zeta\nZ"
    )
    assert frame.items == (
        timeline[0],
        history[0],
        history[1],
        timeline[1],
    )
    assert history == [
        ChatMessage(role="user", content="one"),
        ChatMessage(role="assistant", content="two"),
    ]
    assert timeline == (
        control(sequence=1, anchor=0),
        control(sequence=2, anchor=2),
    )


def test_render_control_message_uses_fixed_attributes_and_escaping() -> None:
    rendered = render_control_message(
        control(sequence=12, anchor=0, content='正文 & <x> "保留"')
    )

    assert rendered == (
        '<mewcode-control\n'
        '  kind="instruction"\n'
        '  scope="round"\n'
        '  sequence="12"\n'
        '  request="2"\n'
        '  round="6">\n'
        '正文 &amp; &lt;x&gt; "保留"\n'
        '</mewcode-control>'
    )


@pytest.mark.parametrize(
    "timeline",
    [
        (control(sequence=2, anchor=0), control(sequence=1, anchor=0)),
        (control(sequence=1, anchor=1), control(sequence=2, anchor=0)),
        (control(sequence=1, anchor=3),),
    ],
)
def test_composer_rejects_invalid_sequence_or_anchor(
    timeline: tuple[ControlMessage, ...],
) -> None:
    composer = PromptComposer(
        (PromptModule("core.identity", 100, "I", "builtin", True),)
    )

    with pytest.raises(ValueError):
        composer.compose(
            [ChatMessage(role="user", content="one")],
            timeline,
        )


def test_session_and_request_controls_omit_inapplicable_attributes() -> None:
    session = ControlMessage(
        "runtime.session",
        "context",
        "session",
        "x",
        1,
        0,
        None,
        None,
    )
    request = ControlMessage(
        "runtime.request_1",
        "instruction",
        "request",
        "x",
        2,
        0,
        1,
        None,
    )

    session_text = render_control_message(session)
    request_text = render_control_message(request)

    assert "request=" not in session_text and "round=" not in session_text
    assert 'request="1"' in request_text and "round=" not in request_text


def test_renderer_defensively_escapes_all_attribute_characters() -> None:
    message = control(sequence=1, anchor=0)
    object.__setattr__(message, "kind", 'x&<>"\'')

    rendered = render_control_message(message)

    assert 'kind="x&amp;&lt;&gt;&quot;&#x27;"' in rendered


def test_compose_is_deterministic() -> None:
    modules = (
        PromptModule("core.identity", 100, "I", "builtin", True),
    )
    history = [ChatMessage(role="user", content="task")]
    timeline = (control(sequence=1, anchor=0),)
    composer = PromptComposer(modules)

    assert composer.compose(history, timeline) == composer.compose(
        history,
        timeline,
    )


def test_runtime_and_composer_api_are_exported_from_prompting_package() -> None:
    from mewcode_agent import prompting

    assert prompting.PromptComposer is PromptComposer
    assert prompting.PromptRuntime is PromptRuntime
    assert prompting.render_control_message is render_control_message
