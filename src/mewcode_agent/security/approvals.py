"""Persistent, project-scoped approval fingerprints."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import tempfile
from typing import Any, cast

import yaml

from mewcode_agent.security._yaml import (
    SecurityConfigError,
    read_yaml_mapping,
    require_exact_keys,
)
from mewcode_agent.security.models import SecurityRule


class PermanentApprovalStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> tuple[SecurityRule, ...]:
        data = read_yaml_mapping(self._path, label="永久审批")
        if data is None:
            return ()
        require_exact_keys(
            data,
            required={"version", "rules"},
            optional=set(),
            location="永久审批配置",
        )
        if type(data["version"]) is not int or data["version"] != 1:
            raise SecurityConfigError("永久审批配置 version 必须为整数 1")
        raw_rules = data["rules"]
        if not isinstance(raw_rules, list):
            raise SecurityConfigError("永久审批配置 rules 必须是列表")
        rules = tuple(
            self._parse_rule(raw, index=index)
            for index, raw in enumerate(raw_rules)
        )
        identifiers = [rule.rule_id for rule in rules]
        if len(identifiers) != len(set(identifiers)):
            raise SecurityConfigError("永久审批配置包含重复规则 id")
        return rules

    def add(self, rule: SecurityRule) -> None:
        if (
            rule.scope != "user"
            or rule.action != "allow"
            or rule.fingerprint is None
            or rule.project_root is None
            or rule.matchers
        ):
            raise ValueError("永久审批规则结构无效")
        rules = list(self.load())
        if any(
            existing.tool_name == rule.tool_name
            and existing.fingerprint == rule.fingerprint
            and existing.project_root == rule.project_root
            for existing in rules
        ):
            return
        rules.append(rule)
        rules.sort(key=lambda item: item.rule_id)
        payload = {
            "version": 1,
            "rules": [
                {
                    "id": item.rule_id,
                    "tool": item.tool_name,
                    "fingerprint": item.fingerprint,
                    "project_root": item.project_root,
                }
                for item in rules
            ],
        }
        temporary_path: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._path.parent,
                delete=False,
                newline="\n",
            ) as temporary:
                yaml.safe_dump(
                    payload,
                    temporary,
                    allow_unicode=True,
                    sort_keys=False,
                )
                temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self._path)
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            try:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SecurityConfigError("无法保存永久工具审批") from exc

    @staticmethod
    def _parse_rule(raw: Any, *, index: int) -> SecurityRule:
        location = f"永久审批配置.rules[{index}]"
        if not isinstance(raw, Mapping):
            raise SecurityConfigError(f"{location} 必须是映射")
        data = cast(Mapping[str, Any], raw)
        require_exact_keys(
            data,
            required={"id", "tool", "fingerprint", "project_root"},
            optional=set(),
            location=location,
        )
        try:
            return SecurityRule(
                rule_id=data["id"],
                scope="user",
                priority=0,
                action="allow",
                tool_name=data["tool"],
                fingerprint=data["fingerprint"],
                project_root=data["project_root"],
            )
        except ValueError as exc:
            raise SecurityConfigError(f"{location} 无效: {exc}") from exc
