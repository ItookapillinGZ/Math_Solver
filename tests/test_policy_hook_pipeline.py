import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.policy import HookPipeline, PolicyHookPipeline


class RecordingAudit:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def record(self, tool_name, decision, *, user_approved=None):
        if self.fail:
            raise OSError("audit unavailable")
        self.calls.append((tool_name, decision, user_approved))


class PolicyHookPipelineTests(unittest.TestCase):
    def test_hook_pipeline_preserves_order_and_short_circuits(self):
        hooks = HookPipeline()
        seen = []
        hooks.register_hook("PreToolUse", lambda _b: seen.append("first"))
        hooks.register_hook("PreToolUse", lambda _b: "blocked")
        hooks.register_hook("PreToolUse", lambda _b: seen.append("third"))

        result = hooks.trigger_hooks("PreToolUse", object())

        self.assertEqual(result, "blocked")
        self.assertEqual(seen, ["first"])

    def test_unknown_event_is_rejected(self):
        hooks = HookPipeline()
        with self.assertRaises(KeyError):
            hooks.register_hook("Unknown", lambda: None)

    def test_allow_decision_is_audited_without_prompting(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = []
            audit = RecordingAudit()
            pipeline = PolicyHookPipeline(
                Path(tmp),
                terminal_print=output.append,
                static_capabilities_for=lambda name: {"shell.execute"}
                if name == "bash" else set(),
                approval_input=lambda _prompt: self.fail("approval should not be requested"),
                audit_logger=audit,
            )
            block = SimpleNamespace(name="bash", input={"command": "echo hello"})

            result = pipeline.trigger_hooks("PreToolUse", block)

            self.assertIsNone(result)
            self.assertEqual(len(audit.calls), 1)
            self.assertEqual(audit.calls[0][0], "bash")
            self.assertEqual(audit.calls[0][1].action.value, "allow")
            self.assertTrue(any("[HOOK] bash" in line for line in output))

    def test_hard_deny_short_circuits_before_log_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = []
            audit = RecordingAudit()
            pipeline = PolicyHookPipeline(
                Path(tmp),
                terminal_print=output.append,
                static_capabilities_for=lambda _name: {"shell.execute"},
                audit_logger=audit,
            )
            block = SimpleNamespace(name="bash", input={"command": "rm -rf /"})

            result = pipeline.trigger_hooks("PreToolUse", block)

            self.assertIn("Permission denied", result)
            self.assertEqual(audit.calls[0][1].action.value, "deny")
            self.assertFalse(any("[HOOK] bash" in line for line in output))

    def test_ask_decision_can_be_approved_and_is_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = []
            prompts = []
            audit = RecordingAudit()
            pipeline = PolicyHookPipeline(
                Path(tmp),
                terminal_print=output.append,
                static_capabilities_for=lambda _name: {"shell.execute"},
                approval_input=lambda prompt: prompts.append(prompt) or "y",
                audit_logger=audit,
            )
            block = SimpleNamespace(name="bash", input={"command": "rm old.txt"})

            result = pipeline.trigger_hooks("PreToolUse", block)

            self.assertIsNone(result)
            self.assertEqual(prompts, ["  Allow? [y/N] "])
            self.assertTrue(audit.calls[0][2])
            self.assertTrue(any("destructive command" in line for line in output))
            self.assertTrue(any("[HOOK] bash" in line for line in output))

    def test_dynamic_mcp_deploy_capability_requires_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = []
            audit = RecordingAudit()
            pipeline = PolicyHookPipeline(
                Path(tmp),
                terminal_print=output.append,
                static_capabilities_for=lambda _name: (),
                approval_input=lambda _prompt: "n",
                audit_logger=audit,
            )
            block = SimpleNamespace(name="mcp__deploy__trigger", input={})

            result = pipeline.trigger_hooks("PreToolUse", block)

            self.assertEqual(result, "Permission denied by user")
            self.assertIn("mcp.invoke", pipeline.resolve_tool_capabilities(block.name))
            self.assertIn("deployment.write", pipeline.resolve_tool_capabilities(block.name))
            self.assertFalse(audit.calls[0][2])
            self.assertTrue(any("MCP destructive-looking tool" in line for line in output))

    def test_audit_failure_does_not_change_permission_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = []
            pipeline = PolicyHookPipeline(
                Path(tmp),
                terminal_print=output.append,
                static_capabilities_for=lambda _name: (),
                audit_logger=RecordingAudit(fail=True),
            )
            block = SimpleNamespace(name="read_file", input={"path": "a.txt"})

            result = pipeline.trigger_hooks("PreToolUse", block)

            self.assertIsNone(result)
            self.assertTrue(any("[policy-audit]" in line for line in output))
            self.assertTrue(any("[HOOK] read_file" in line for line in output))

    def test_large_output_and_stop_hooks_emit_observability_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = []
            pipeline = PolicyHookPipeline(
                Path(tmp),
                terminal_print=output.append,
                large_output_threshold=3,
            )
            block = SimpleNamespace(name="read_file", input={})

            pipeline.trigger_hooks("PostToolUse", block, "1234")
            pipeline.trigger_hooks(
                "Stop",
                [
                    {"role": "user", "content": [
                        {"type": "tool_result", "content": "a"},
                        {"type": "tool_result", "content": "b"},
                    ]},
                    {"role": "assistant", "content": "done"},
                ],
            )

            self.assertTrue(any("large output from read_file: 4 chars" in line for line in output))
            self.assertTrue(any("Stop: 2 tool result(s)" in line for line in output))

    def test_default_audit_logger_writes_jsonl_without_raw_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pipeline = PolicyHookPipeline(
                root,
                terminal_print=lambda _text: None,
                static_capabilities_for=lambda _name: {"shell.execute"},
            )
            secret_command = "echo super-secret-value"
            block = SimpleNamespace(name="bash", input={"command": secret_command})

            self.assertIsNone(pipeline.trigger_hooks("PreToolUse", block))

            audit_path = root / ".audit" / "policy.jsonl"
            payload = audit_path.read_text(encoding="utf-8")
            record = json.loads(payload.splitlines()[-1])
            self.assertEqual(record["tool_name"], "bash")
            self.assertNotIn(secret_command, payload)


if __name__ == "__main__":
    unittest.main()
