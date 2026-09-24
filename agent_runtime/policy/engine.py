from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .models import PermissionDecision, PolicyContext, PolicyRule
from .rules import (
    DEFAULT_APPROVAL_PATTERNS,
    DEFAULT_DENY_PATTERNS,
    build_default_rules,
)


class PolicyEngine:
    """Evaluate tool calls against an ordered list of permission rules.

    The runtime supplies declared tool capabilities for each call. Default
    rules are capability-driven; the first matching rule wins. The engine
    performs no user interaction.
    """

    def __init__(
        self,
        workspace: Path,
        deny_patterns: Iterable[str] = DEFAULT_DENY_PATTERNS,
        approval_patterns: Iterable[str] = DEFAULT_APPROVAL_PATTERNS,
        rules: Sequence[PolicyRule] | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.rules = tuple(rules) if rules is not None else build_default_rules(
            deny_patterns=deny_patterns,
            approval_patterns=approval_patterns,
        )

    def evaluate(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None,
        *,
        capabilities: Iterable[str] = (),
    ) -> PermissionDecision:
        context = PolicyContext(
            tool_name=tool_name,
            tool_input=tool_input if isinstance(tool_input, dict) else {},
            workspace=self.workspace,
            capabilities=frozenset(capabilities),
        )

        for rule in self.rules:
            if rule.matches(context):
                return rule.decision(context)

        return PermissionDecision.allow()
