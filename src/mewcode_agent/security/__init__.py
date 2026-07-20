"""Public security policy API."""

from mewcode_agent.security._yaml import SecurityConfigError
from mewcode_agent.security.approvals import PermanentApprovalStore
from mewcode_agent.security.boundary import SecurityBoundary
from mewcode_agent.security.command_guard import DangerousCommandGuard
from mewcode_agent.security.loader import load_security_configuration
from mewcode_agent.security.models import (
    ArgumentMatcher,
    MatcherKind,
    PermissionMode,
    PolicyDecision,
    RuleScope,
    SecurityAction,
    SecurityConfiguration,
    SecurityPolicyStatus,
    SecurityRequest,
    SecurityRule,
)
from mewcode_agent.security.path_sandbox import (
    PathSandbox,
    PathSandboxError,
)
from mewcode_agent.security.policy import SecurityPolicyEngine

__all__ = [
    "ArgumentMatcher",
    "DangerousCommandGuard",
    "MatcherKind",
    "PathSandbox",
    "PathSandboxError",
    "PermissionMode",
    "PermanentApprovalStore",
    "PolicyDecision",
    "RuleScope",
    "SecurityAction",
    "SecurityBoundary",
    "SecurityConfigError",
    "SecurityConfiguration",
    "SecurityPolicyEngine",
    "SecurityPolicyStatus",
    "SecurityRequest",
    "SecurityRule",
    "load_security_configuration",
]
