from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from mewcode_agent.commands import (
    CommandRegistrationError,
    CommandRegistry,
    CommandSpec,
)


async def no_op_handler(_invocation: object, _ui: object) -> None:
    return None


def make_spec(
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    category: str = "general",
    hidden: bool = False,
    status_hint: bool = False,
    handler: Callable[[object, object], Awaitable[None]] = no_op_handler,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        aliases=aliases,
        description=f"{name} description",
        usage=f"/{name}",
        execution_kind="local",
        category=category,  # type: ignore[arg-type]
        argument_hint="",
        handler=handler,  # type: ignore[arg-type]
        hidden=hidden,
        status_hint=status_hint,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "Upper"},
        {"name": "bad_name"},
        {"name": "-bad"},
        {"name": "help", "aliases": ("H",)},
        {"name": "help", "aliases": ("help",)},
        {"name": "help", "description": "bad\ntext"},
        {"name": "help", "usage": "/other"},
        {"name": "help", "execution_kind": "remote"},
        {"name": "help", "category": "other"},
        {"name": "help", "argument_hint": "bad\rtext"},
        {"name": "help", "handler": None},
        {"name": "help", "hidden": True, "status_hint": True},
    ],
)
def test_invalid_command_metadata_is_rejected(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "name": "help",
        "aliases": (),
        "description": "description",
        "usage": "/help",
        "execution_kind": "local",
        "category": "general",
        "argument_hint": "",
        "handler": no_op_handler,
        "hidden": False,
        "status_hint": False,
    }
    values.update(kwargs)

    with pytest.raises(CommandRegistrationError) as captured:
        CommandSpec(**values)  # type: ignore[arg-type]

    assert captured.value.code == "command_registry_invalid"


@pytest.mark.parametrize(
    "first,second,conflict",
    [
        (make_spec("alpha"), make_spec("alpha"), "alpha"),
        (make_spec("alpha", aliases=("a",)), make_spec("a"), "a"),
        (make_spec("alpha"), make_spec("beta", aliases=("alpha",)), "alpha"),
        (
            make_spec("alpha", aliases=("a",)),
            make_spec("beta", aliases=("a",)),
            "a",
        ),
    ],
)
def test_registry_rejects_all_key_conflicts_atomically(
    first: CommandSpec,
    second: CommandSpec,
    conflict: str,
) -> None:
    registry = CommandRegistry()
    registry.register(first)

    with pytest.raises(CommandRegistrationError) as captured:
        registry.register(second)

    assert conflict in str(captured.value)
    assert registry.public_specs() == (first,)
    assert registry.resolve(second.name) is (
        first if second.name in (first.name, *first.aliases) else None
    )


def test_registry_resolves_names_and_aliases_case_insensitively() -> None:
    registry = CommandRegistry()
    spec = make_spec("help", aliases=("h", "?"))
    registry.register(spec)

    assert registry.resolve("HELP") is spec
    assert registry.resolve("H") is spec
    assert registry.resolve("?") is spec
    assert registry.resolve("missing") is None


def test_public_catalog_groups_categories_and_preserves_registration_order() -> None:
    registry = CommandRegistry()
    workflow_first = make_spec("review", category="workflow")
    general_first = make_spec("help", category="general")
    hidden = make_spec("internal", hidden=True)
    general_second = make_spec("status", category="general")
    for spec in (workflow_first, general_first, hidden, general_second):
        registry.register(spec)

    assert registry.public_specs() == (
        general_first,
        general_second,
        workflow_first,
    )


def test_completion_uses_public_names_and_aliases_in_declared_order() -> None:
    registry = CommandRegistry()
    registry.register(make_spec("help", aliases=("h", "?")))
    registry.register(make_spec("hidden", aliases=("hide",), hidden=True))
    registry.register(make_spec("history", aliases=("hist",)))

    assert registry.completion_candidates("H") == (
        "help",
        "h",
        "history",
        "hist",
    )
    assert registry.completion_candidates("") == (
        "help",
        "h",
        "?",
        "history",
        "hist",
    )


def test_status_hints_use_only_marked_public_canonical_names() -> None:
    registry = CommandRegistry()
    registry.register(make_spec("help", aliases=("h",), status_hint=True))
    registry.register(make_spec("status"))

    assert registry.status_hints() == ("/help",)


def test_freeze_is_idempotent_and_rejects_later_registration() -> None:
    registry = CommandRegistry()
    registry.register(make_spec("help"))
    registry.freeze()
    registry.freeze()

    with pytest.raises(CommandRegistrationError) as captured:
        registry.register(make_spec("status"))

    assert captured.value.code == "command_registry_invalid"
    assert registry.frozen is True
    assert registry.resolve("help") is not None


def test_dynamic_commands_replace_atomically_after_freeze() -> None:
    registry = CommandRegistry()
    fixed = make_spec("help")
    first = make_spec("alpha", category="workflow")
    second = make_spec("beta", category="workflow")
    registry.register(fixed)
    registry.freeze()

    registry.replace_dynamic((first,))
    assert registry.resolve("alpha") is first
    assert registry.public_specs() == (fixed, first)

    registry.replace_dynamic((second,))
    assert registry.resolve("alpha") is None
    assert registry.resolve("beta") is second
    assert registry.public_specs() == (fixed, second)


def test_dynamic_command_conflict_validation_has_zero_mutation() -> None:
    registry = CommandRegistry()
    fixed = make_spec("help", aliases=("h",))
    current = make_spec("alpha")
    registry.register(fixed)
    registry.freeze()
    registry.replace_dynamic((current,))

    with pytest.raises(CommandRegistrationError, match="h"):
        registry.validate_dynamic((make_spec("beta", aliases=("h",)),))

    assert registry.resolve("alpha") is current
    assert registry.resolve("beta") is None


def test_dynamic_commands_require_frozen_registry() -> None:
    registry = CommandRegistry()

    with pytest.raises(CommandRegistrationError, match="冻结"):
        registry.replace_dynamic((make_spec("alpha"),))
