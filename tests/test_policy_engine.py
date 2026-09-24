import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_runtime.policy import (
    PermissionAction,
    PolicyEngine,
    PolicyRule,
    exact_tools,
)


class PolicyEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.engine = PolicyEngine(self.workspace)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_normal_shell_is_allowed(self):
        decision = self.engine.evaluate(
            "bash",
            {"command": "echo hello"},
            capabilities={"shell.execute"},
        )
        self.assertEqual(decision.action, PermissionAction.ALLOW)
        self.assertEqual(decision.rule_id, "default.allow")

    def test_hard_deny_rule_wins(self):
        decision = self.engine.evaluate(
            "bash",
            {"command": "sudo whoami"},
            capabilities={"shell.execute"},
        )
        self.assertEqual(decision.action, PermissionAction.DENY)
        self.assertEqual(decision.rule_id, "shell.hard_deny")

    def test_destructive_shell_requires_approval(self):
        decision = self.engine.evaluate(
            "bash",
            {"command": "rm temp.txt"},
            capabilities={"shell.execute"},
        )
        self.assertEqual(decision.action, PermissionAction.ASK)
        self.assertEqual(
            decision.rule_id,
            "shell.destructive_requires_approval",
        )

    def test_workspace_write_is_allowed(self):
        decision = self.engine.evaluate(
            "write_file",
            {"path": "notes/test.txt"},
            capabilities={"filesystem.write"},
        )
        self.assertEqual(decision.action, PermissionAction.ALLOW)

    def test_workspace_escape_is_denied(self):
        decision = self.engine.evaluate(
            "write_file",
            {"path": "../outside.txt"},
            capabilities={"filesystem.write"},
        )
        self.assertEqual(decision.action, PermissionAction.DENY)
        self.assertEqual(decision.rule_id, "filesystem.workspace_escape")

    def test_deployment_capability_requires_approval(self):
        decision = self.engine.evaluate(
            "mcp__prod__deploy",
            {},
            capabilities={"mcp.invoke", "deployment.write"},
        )
        self.assertEqual(decision.action, PermissionAction.ASK)
        self.assertEqual(
            decision.rule_id,
            "deployment.write_requires_approval",
        )

    def test_capability_not_tool_name_drives_shell_rule(self):
        decision = self.engine.evaluate(
            "custom_command_runner",
            {"command": "sudo whoami"},
            capabilities={"shell.execute"},
        )
        self.assertEqual(decision.action, PermissionAction.DENY)
        self.assertEqual(decision.rule_id, "shell.hard_deny")

    def test_same_name_without_capability_is_not_treated_as_shell(self):
        decision = self.engine.evaluate(
            "bash",
            {"command": "sudo whoami"},
            capabilities=set(),
        )
        self.assertEqual(decision.action, PermissionAction.ALLOW)

    def test_first_matching_custom_rule_wins(self):
        rules = (
            PolicyRule(
                rule_id="test.first",
                action=PermissionAction.DENY,
                tool_matcher=exact_tools("bash"),
                condition=lambda _context: True,
                reason="first",
            ),
            PolicyRule(
                rule_id="test.second",
                action=PermissionAction.ALLOW,
                tool_matcher=exact_tools("bash"),
                condition=lambda _context: True,
                reason="second",
            ),
        )
        engine = PolicyEngine(self.workspace, rules=rules)
        decision = engine.evaluate("bash", {"command": "echo hello"})
        self.assertEqual(decision.action, PermissionAction.DENY)
        self.assertEqual(decision.rule_id, "test.first")


if __name__ == "__main__":
    unittest.main()
