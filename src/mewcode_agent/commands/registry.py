"""Central command registration, exact lookup, and completion catalog."""

from __future__ import annotations

from mewcode_agent.commands.models import (
    COMMAND_CATEGORIES,
    CommandRegistrationError,
    CommandSpec,
)


class CommandRegistry:
    def __init__(self) -> None:
        self._specs: list[CommandSpec] = []
        self._lookup: dict[str, CommandSpec] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, spec: CommandSpec) -> None:
        if self._frozen:
            raise CommandRegistrationError("命令注册中心已经冻结")
        if not isinstance(spec, CommandSpec):
            raise CommandRegistrationError("命令 spec 类型无效")
        keys = (spec.name, *spec.aliases)
        conflict = next((key for key in keys if key in self._lookup), None)
        if conflict is not None:
            raise CommandRegistrationError(f"命令 key 冲突：{conflict}")
        self._specs.append(spec)
        for key in keys:
            self._lookup[key] = spec

    def freeze(self) -> None:
        self._frozen = True

    def resolve(self, name: str) -> CommandSpec | None:
        if not isinstance(name, str):
            raise TypeError("name 必须是字符串")
        return self._lookup.get(name.lower())

    def public_specs(self) -> tuple[CommandSpec, ...]:
        return tuple(
            spec
            for category in COMMAND_CATEGORIES
            for spec in self._specs
            if not spec.hidden and spec.category == category
        )

    def completion_candidates(self, prefix: str) -> tuple[str, ...]:
        if not isinstance(prefix, str):
            raise TypeError("prefix 必须是字符串")
        normalized = prefix.lower()
        return tuple(
            key
            for spec in self._specs
            if not spec.hidden
            for key in (spec.name, *spec.aliases)
            if key.startswith(normalized)
        )

    def status_hints(self) -> tuple[str, ...]:
        return tuple(
            f"/{spec.name}"
            for spec in self._specs
            if not spec.hidden and spec.status_hint
        )

