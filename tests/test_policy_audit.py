import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_runtime.policy import (
    PermissionDecision,
    PolicyAuditLogger,
)


class PolicyAuditLoggerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.audit_path = Path(self.temp_dir.name) / ".audit" / "policy.jsonl"
        self.logger = PolicyAuditLogger(self.audit_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_allow_event_is_written_as_jsonl(self):
        event = self.logger.record(
            "read_file",
            PermissionDecision.allow(rule_id="default.allow"),
        )

        self.assertTrue(self.audit_path.exists())
        record = json.loads(self.audit_path.read_text(encoding="utf-8").strip())
        self.assertEqual(record["tool_name"], "read_file")
        self.assertEqual(record["action"], "allow")
        self.assertEqual(record["outcome"], "allowed")
        self.assertIsNone(record["user_approved"])
        self.assertEqual(event.rule_id, "default.allow")

    def test_ask_event_records_user_approval(self):
        decision = PermissionDecision.ask(
            reason="destructive command",
            rule_id="bash.destructive_requires_approval",
        )
        self.logger.record("bash", decision, user_approved=True)
        self.logger.record("bash", decision, user_approved=False)

        records = self.logger.read_recent(limit=10)
        self.assertEqual(records[0]["outcome"], "approved_by_user")
        self.assertTrue(records[0]["user_approved"])
        self.assertEqual(records[1]["outcome"], "denied_by_user")
        self.assertFalse(records[1]["user_approved"])

    def test_deny_event_records_policy_rule(self):
        decision = PermissionDecision.deny(
            reason="'sudo' is on the deny list",
            rule_id="bash.hard_deny",
        )
        self.logger.record("bash", decision)

        record = self.logger.read_recent(limit=1)[0]
        self.assertEqual(record["rule_id"], "bash.hard_deny")
        self.assertEqual(record["action"], "deny")
        self.assertEqual(record["outcome"], "denied_by_policy")

    def test_read_recent_returns_only_requested_tail(self):
        for index in range(5):
            self.logger.record(
                f"tool_{index}",
                PermissionDecision.allow(),
            )

        records = self.logger.read_recent(limit=2)
        self.assertEqual([r["tool_name"] for r in records], ["tool_3", "tool_4"])


if __name__ == "__main__":
    unittest.main()
