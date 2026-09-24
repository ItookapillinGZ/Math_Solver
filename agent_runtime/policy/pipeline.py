from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .audit import PolicyAuditLogger
from .engine import PolicyEngine
from .models import PermissionAction, PermissionDecision


DEFAULT_HOOK_EVENTS = (
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
)


class HookPipeline:
    """Small ordered hook registry used by agent runtimes.

    Hooks run in registration order. The first callback that returns a
    non-``None`` value short-circuits the event, preserving the behavior of
    the original teaching loop where PreToolUse could block execution.
    """

    def __init__(self, events: Iterable[str] = DEFAULT_HOOK_EVENTS):
        self.hooks: dict[str, list[Callable[..., Any]]] = {
            event: [] for event in events
        }

    def register_hook(self, event: str, callback: Callable[..., Any]) -> None:
        if event not in self.hooks:
            raise KeyError(f"Unknown hook event: {event}")
        self.hooks[event].append(callback)

    def trigger_hooks(self, event: str, *args: Any) -> Any:
        if event not in self.hooks:
            raise KeyError(f"Unknown hook event: {event}")
        for callback in tuple(self.hooks[event]):
            result = callback(*args)
            if result is not None:
                return result
        return None


class PolicyHookPipeline:
    """Runtime middleware joining hooks, policy decisions, and audit logging.

    The pure :class:`PolicyEngine` remains responsible only for deciding
    ALLOW/DENY/ASK. This class owns the runtime concerns around that decision:
    capability resolution, interactive approval, best-effort audit logging,
    and the default logging/large-output/stop hooks.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        terminal_print: Callable[[str], None],
        static_capabilities_for: Callable[[str], Iterable[str]] | None = None,
        approval_input: Callable[[str], str] = input,
        policy_engine: PolicyEngine | None = None,
        audit_logger: PolicyAuditLogger | None = None,
        large_output_threshold: int = 100_000,
        decision_sink: Callable[[dict], None] | None = None,
    ):
        self.workspace = Path(workspace)
        self.terminal_print = terminal_print
        self.static_capabilities_for = static_capabilities_for or (
            lambda _tool_name: ()
        )
        self.approval_input = approval_input
        self.policy_engine = policy_engine or PolicyEngine(self.workspace)
        self.audit_logger = audit_logger or PolicyAuditLogger(
            self.workspace / ".audit" / "policy.jsonl"
        )
        self.large_output_threshold = int(large_output_threshold)
        self.decision_sink = decision_sink
        self.pipeline = HookPipeline()
        self._install_default_hooks()

    @property
    def hooks(self) -> dict[str, list[Callable[..., Any]]]:
        """Expose the registry for backward-compatible inspection."""
        return self.pipeline.hooks

    def register_hook(self, event: str, callback: Callable[..., Any]) -> None:
        self.pipeline.register_hook(event, callback)

    def trigger_hooks(self, event: str, *args: Any) -> Any:
        return self.pipeline.trigger_hooks(event, *args)

    def resolve_tool_capabilities(self, tool_name: str) -> frozenset[str]:
        capabilities = set(self.static_capabilities_for(tool_name) or ())

        # MCP tools are discovered dynamically and therefore are not always
        # represented by static ToolDefinition objects.
        if tool_name.startswith("mcp__"):
            capabilities.add("mcp.invoke")
            if "deploy" in tool_name:
                capabilities.add("deployment.write")

        if tool_name == "compact":
            capabilities.add("context.write")

        return frozenset(capabilities)

    def permission_hook(self, block: Any) -> str | None:
        if not isinstance(getattr(block, "input", None), dict):
            block.input = {}

        decision = self.policy_engine.evaluate(
            block.name,
            block.input,
            capabilities=self.resolve_tool_capabilities(block.name),
        )

        if decision.action is PermissionAction.ALLOW:
            self._record_policy_audit(block, decision)
            return None

        if decision.action is PermissionAction.DENY:
            self._record_policy_audit(block, decision)
            return f"Permission denied: {decision.reason}"

        self._print_approval_request(block)
        choice = self.approval_input("  Allow? [y/N] ").strip().lower()
        approved = choice in ("y", "yes")
        self._record_policy_audit(
            block,
            decision,
            user_approved=approved,
        )
        if not approved:
            return "Permission denied by user"
        return None

    def log_hook(self, block: Any) -> None:
        self.terminal_print(f"\033[90m[HOOK] {block.name}\033[0m")
        return None

    def large_output_hook(self, block: Any, output: Any) -> None:
        size = len(str(output))
        if size > self.large_output_threshold:
            self.terminal_print(
                f"\033[33m[HOOK] large output from {block.name}: "
                f"{size} chars\033[0m"
            )
        return None

    def user_prompt_hook(self, _query: str) -> None:
        self.terminal_print(
            f"\033[90m[HOOK] UserPromptSubmit: {self.workspace}\033[0m"
        )
        return None

    def stop_hook(self, messages: list[dict]) -> None:
        tool_count = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                tool_count += sum(
                    1
                    for item in content
                    if isinstance(item, dict)
                    and item.get("type") == "tool_result"
                )
        self.terminal_print(
            f"\033[90m[HOOK] Stop: {tool_count} tool result(s)\033[0m"
        )
        return None

    def _install_default_hooks(self) -> None:
        self.register_hook("UserPromptSubmit", self.user_prompt_hook)
        self.register_hook("PreToolUse", self.permission_hook)
        self.register_hook("PreToolUse", self.log_hook)
        self.register_hook("PostToolUse", self.large_output_hook)
        self.register_hook("Stop", self.stop_hook)

    def _record_policy_audit(
        self,
        block: Any,
        decision: PermissionDecision,
        *,
        user_approved: bool | None = None,
    ) -> None:
        # Audit is intentionally best-effort. A logging failure must not change
        # the permission outcome or make an otherwise valid tool call fail.
        try:
            self.audit_logger.record(
                block.name,
                decision,
                user_approved=user_approved,
            )
        except Exception as exc:
            self.terminal_print(
                f"\033[33m[policy-audit] failed to write audit log: "
                f"{exc}\033[0m"
            )

        if self.decision_sink is not None:
            try:
                self.decision_sink(
                    {
                        "tool_name": block.name,
                        "action": decision.action.value,
                        "reason": decision.reason,
                        "user_approved": user_approved,
                    }
                )
            except Exception:
                pass

    def _print_approval_request(self, block: Any) -> None:
        if block.name == "bash":
            command = block.input.get("command", "")
            self.terminal_print(
                "\n\033[33m[permission] destructive command\033[0m"
            )
            self.terminal_print(f"  {command}")
        elif block.name.startswith("mcp__"):
            self.terminal_print(
                "\n\033[33m[permission] MCP destructive-looking tool: "
                f"{block.name}\033[0m"
            )
        else:
            self.terminal_print(
                "\n\033[33m[permission] approval required: "
                f"{block.name}\033[0m"
            )
