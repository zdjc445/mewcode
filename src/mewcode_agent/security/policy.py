"""Deterministic layered security policy evaluation."""

from __future__ import annotations

from fnmatch import fnmatchcase
from hashlib import sha256
import json
import os
from typing import Any

from mewcode_agent.security.approvals import PermanentApprovalStore
from mewcode_agent.security.boundary import SecurityBoundary
from mewcode_agent.security.models import (
    ArgumentMatcher,
    PermissionMode,
    PolicyDecision,
    RuleScope,
    SecurityConfiguration,
    SecurityPolicyStatus,
    SecurityRequest,
    SecurityRule,
)


_ACTION_ORDER = {"deny": 2, "ask": 1, "allow": 0}
_IGNORED_FINGERPRINT_ARGUMENTS = {
    "write_file": {"content"},
    "edit_file": {"edits"},
}


class SecurityPolicyEngine:
    def __init__(
        self,
        configuration: SecurityConfiguration,
        boundary: SecurityBoundary,
        *,
        approval_store: PermanentApprovalStore | None = None,
    ) -> None:
        self._configuration = configuration
        self._boundary = boundary
        self._approval_store = approval_store
        self._session_rules: list[SecurityRule] = []
        self._permanent_rules = list(configuration.permanent_rules)
        self._mode_override: PermissionMode | None = None

    @property
    def mode(self) -> PermissionMode:
        return self._mode_override or self._configuration.mode

    @property
    def configured_mode(self) -> PermissionMode:
        return self._configuration.mode

    @property
    def boundary(self) -> SecurityBoundary:
        return self._boundary

    def evaluate(self, request: SecurityRequest) -> PolicyDecision:
        boundary_decision = self._boundary.evaluate(request)
        if boundary_decision is not None:
            return boundary_decision

        fingerprint = self.fingerprint(request)
        layers: tuple[tuple[RuleScope, tuple[SecurityRule, ...]], ...] = (
            ("session", tuple(self._session_rules)),
            ("project", self._configuration.project_rules),
            (
                "user",
                tuple(self._permanent_rules) + self._configuration.user_rules,
            ),
        )
        for scope, rules in layers:
            matched = self._select_rule(request, fingerprint, rules)
            if matched is not None:
                return PolicyDecision(
                    matched.action,
                    "matched_rule",
                    matched.rule_id,
                    scope,
                )

        if (
            request.current_request_authorized
            and request.category in ("write", "command")
        ):
            return PolicyDecision("allow", "request_authorized")
        if self.mode == "strict":
            return PolicyDecision("ask", "strict_mode_default")
        if self.mode == "default":
            action = "allow" if request.category == "read" else "ask"
            return PolicyDecision(action, "default_mode_default")
        return PolicyDecision("allow", "permissive_mode_default")

    def set_mode_override(self, mode: PermissionMode | None) -> None:
        if mode is not None and mode not in (
            "strict",
            "default",
            "permissive",
        ):
            raise ValueError("permission mode override 无效")
        self._mode_override = mode

    def status(self) -> SecurityPolicyStatus:
        return SecurityPolicyStatus(
            self._configuration.mode,
            self.mode,
            self._mode_override is not None,
            len(self._configuration.user_rules),
            len(self._configuration.project_rules),
            len(self._permanent_rules),
            len(self._session_rules),
        )

    def allow_for_session(self, request: SecurityRequest) -> None:
        rule = self._approval_rule(request, scope="session")
        if not any(
            existing.fingerprint == rule.fingerprint
            and existing.tool_name == rule.tool_name
            for existing in self._session_rules
        ):
            self._session_rules.append(rule)

    def allow_permanently(self, request: SecurityRequest) -> None:
        if self._approval_store is None:
            raise RuntimeError("永久审批存储未配置")
        rule = self._approval_rule(request, scope="user")
        self._approval_store.add(rule)
        if not any(
            existing.fingerprint == rule.fingerprint
            and existing.tool_name == rule.tool_name
            and existing.project_root == rule.project_root
            for existing in self._permanent_rules
        ):
            self._permanent_rules.append(rule)

    def fingerprint(self, request: SecurityRequest) -> str:
        ignored = _IGNORED_FINGERPRINT_ARGUMENTS.get(request.tool_name, set())
        normalized_arguments: dict[str, Any] = {}
        for key in sorted(request.arguments):
            if key in ignored:
                continue
            value = request.arguments[key]
            if (
                isinstance(value, str)
                and self._boundary.is_path_argument(request.tool_name, key)
            ):
                normalized = self._boundary.normalized_path_argument(
                    request,
                    key,
                )
                normalized_arguments[key] = normalized
            else:
                normalized_arguments[key] = value
        canonical = json.dumps(
            {
                "tool": request.tool_name,
                "project_root": str(request.working_directory),
                "arguments": normalized_arguments,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def _approval_rule(
        self,
        request: SecurityRequest,
        *,
        scope: RuleScope,
    ) -> SecurityRule:
        fingerprint = self.fingerprint(request)
        return SecurityRule(
            rule_id=f"approval.h_{fingerprint[:24]}",
            scope=scope,
            priority=0,
            action="allow",
            tool_name=request.tool_name,
            fingerprint=fingerprint,
            project_root=str(request.working_directory),
        )

    def _select_rule(
        self,
        request: SecurityRequest,
        fingerprint: str,
        rules: tuple[SecurityRule, ...],
    ) -> SecurityRule | None:
        matching = [
            rule
            for rule in rules
            if self._matches(rule, request, fingerprint)
        ]
        if not matching:
            return None
        return sorted(
            matching,
            key=lambda rule: (
                -rule.priority,
                -_ACTION_ORDER[rule.action],
                rule.rule_id,
            ),
        )[0]

    def _matches(
        self,
        rule: SecurityRule,
        request: SecurityRequest,
        fingerprint: str,
    ) -> bool:
        if rule.tool_name != request.tool_name:
            return False
        if rule.project_root is not None and os.path.normcase(
            rule.project_root
        ) != os.path.normcase(str(request.working_directory)):
            return False
        if rule.fingerprint is not None:
            return rule.fingerprint == fingerprint
        return all(
            self._matcher_matches(matcher, request)
            for matcher in rule.matchers
        )

    def _matcher_matches(
        self,
        matcher: ArgumentMatcher,
        request: SecurityRequest,
    ) -> bool:
        if matcher.kind == "path_glob":
            if not self._boundary.is_path_argument(
                request.tool_name,
                matcher.argument,
            ):
                return False
            value = self._boundary.normalized_path_argument(
                request,
                matcher.argument,
            )
            return value is not None and _path_glob_match(
                value,
                str(matcher.pattern),
            )
        if matcher.argument not in request.arguments:
            return False
        value = request.arguments[matcher.argument]
        if matcher.kind == "exact":
            return type(value) is type(matcher.pattern) and value == matcher.pattern
        return isinstance(value, str) and fnmatchcase(value, str(matcher.pattern))


def _path_glob_match(value: str, pattern: str) -> bool:
    value_parts = tuple(part for part in value.split("/") if part)
    normalized_pattern = pattern.replace("\\", "/")
    pattern_parts = tuple(
        part for part in normalized_pattern.split("/") if part
    )

    def match(pattern_index: int, value_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return value_index == len(value_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return any(
                match(pattern_index + 1, next_value_index)
                for next_value_index in range(
                    value_index,
                    len(value_parts) + 1,
                )
            )
        if value_index == len(value_parts):
            return False
        return fnmatchcase(value_parts[value_index], part) and match(
            pattern_index + 1,
            value_index + 1,
        )

    return match(0, 0)
