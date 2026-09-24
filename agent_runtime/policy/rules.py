from collections.abc import Iterable
from typing import Callable

from .models import PermissionAction, PolicyContext, PolicyRule, RuleCondition


DEFAULT_DENY_PATTERNS = (
    "rm -rf /",
    "sudo",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
)

DEFAULT_APPROVAL_PATTERNS = (
    "rm ",
    "> /etc/",
    "chmod 777",
)


def any_tool(_tool_name: str) -> bool:
    return True


def exact_tools(*names: str) -> Callable[[str], bool]:
    allowed = frozenset(names)
    return lambda tool_name: tool_name in allowed


def tool_prefix(prefix: str) -> Callable[[str], bool]:
    return lambda tool_name: tool_name.startswith(prefix)


def all_conditions(*conditions: RuleCondition) -> RuleCondition:
    return lambda context: all(condition(context) for condition in conditions)


def has_capability(capability: str) -> RuleCondition:
    return lambda context: capability in context.capabilities


def _command_contains(pattern: str) -> RuleCondition:
    return lambda context: pattern in str(context.tool_input.get("command", ""))


def _workspace_escape(context: PolicyContext) -> bool:
    # Only path-bearing calls can escape the workspace. Other tools may also
    # possess filesystem.write but use internally controlled destinations.
    if "path" not in context.tool_input:
        return False
    raw_path = str(context.tool_input.get("path", ""))
    candidate = (context.workspace / raw_path).resolve()
    return not candidate.is_relative_to(context.workspace)


def _workspace_escape_reason(context: PolicyContext) -> str:
    return f"path escapes workspace: {context.tool_input.get('path', '')}"


def build_default_rules(
    deny_patterns: Iterable[str] = DEFAULT_DENY_PATTERNS,
    approval_patterns: Iterable[str] = DEFAULT_APPROVAL_PATTERNS,
) -> tuple[PolicyRule, ...]:
    """Build ordered capability-based permission rules.

    Tool names are no longer the primary security boundary. A tool declares
    capabilities in ToolDefinition and the runtime passes those capabilities
    into PolicyEngine.evaluate(). The first matching rule still wins.
    """

    rules: list[PolicyRule] = []

    for pattern in deny_patterns:
        rules.append(
            PolicyRule(
                rule_id="shell.hard_deny",
                action=PermissionAction.DENY,
                tool_matcher=any_tool,
                condition=all_conditions(
                    has_capability("shell.execute"),
                    _command_contains(pattern),
                ),
                reason=lambda _context, p=pattern: f"'{p}' is on the deny list",
            )
        )

    for pattern in approval_patterns:
        rules.append(
            PolicyRule(
                rule_id="shell.destructive_requires_approval",
                action=PermissionAction.ASK,
                tool_matcher=any_tool,
                condition=all_conditions(
                    has_capability("shell.execute"),
                    _command_contains(pattern),
                ),
                reason=lambda _context, p=pattern: (
                    f"destructive command matched '{p}'"
                ),
            )
        )

    rules.append(
        PolicyRule(
            rule_id="filesystem.workspace_escape",
            action=PermissionAction.DENY,
            tool_matcher=any_tool,
            condition=all_conditions(
                has_capability("filesystem.write"),
                _workspace_escape,
            ),
            reason=_workspace_escape_reason,
        )
    )

    rules.append(
        PolicyRule(
            rule_id="deployment.write_requires_approval",
            action=PermissionAction.ASK,
            tool_matcher=any_tool,
            condition=has_capability("deployment.write"),
            reason=lambda context: (
                f"deployment-capable tool requires approval: {context.tool_name}"
            ),
        )
    )

    return tuple(rules)
