from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, TypeAlias


class PermissionAction(str, Enum):
    """Possible outcomes of a policy evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class PermissionDecision:
    """Structured result returned by the policy engine."""

    action: PermissionAction
    reason: str = ""
    rule_id: str = ""

    @classmethod
    def allow(cls, reason: str = "", rule_id: str = "default.allow"):
        return cls(PermissionAction.ALLOW, reason, rule_id)

    @classmethod
    def deny(cls, reason: str, rule_id: str):
        return cls(PermissionAction.DENY, reason, rule_id)

    @classmethod
    def ask(cls, reason: str, rule_id: str):
        return cls(PermissionAction.ASK, reason, rule_id)


@dataclass(frozen=True)
class PolicyContext:
    """Normalized input presented to every policy rule."""

    tool_name: str
    tool_input: Mapping[str, Any]
    workspace: Path
    capabilities: frozenset[str] = field(default_factory=frozenset)


ToolMatcher: TypeAlias = Callable[[str], bool]
RuleCondition: TypeAlias = Callable[[PolicyContext], bool]
ReasonFactory: TypeAlias = Callable[[PolicyContext], str]


@dataclass(frozen=True)
class PolicyRule:
    """One ordered permission rule.

    Rules still support a tool-name matcher for exceptional cases, but default
    runtime policy is capability-driven. The first matching rule wins.
    """

    rule_id: str
    action: PermissionAction
    tool_matcher: ToolMatcher
    condition: RuleCondition
    reason: str | ReasonFactory

    def matches(self, context: PolicyContext) -> bool:
        return self.tool_matcher(context.tool_name) and self.condition(context)

    def decision(self, context: PolicyContext) -> PermissionDecision:
        reason = self.reason(context) if callable(self.reason) else self.reason
        return PermissionDecision(
            action=self.action,
            reason=reason,
            rule_id=self.rule_id,
        )
