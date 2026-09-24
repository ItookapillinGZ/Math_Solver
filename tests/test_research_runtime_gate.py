from __future__ import annotations

import json
import unittest

from tests.test_agent_runtime import FakeBlock, FakeResponse, RuntimeHarness


def tool(action, payload=None):
    return FakeResponse([FakeBlock(
        "tool_use", name="research_workflow",
        tool_input={"action": action, "payload": payload or {}},
    )])


def prose(text="unverified proof"):
    return FakeResponse([FakeBlock("text", text=text)])


class ResearchRuntimeGateTests(unittest.TestCase):
    def make_harness(self, responses):
        harness = RuntimeHarness(responses)
        stages = {}
        calls = []

        def workflow(action, payload=None):
            payload = payload or {}
            calls.append((action, dict(payload)))
            if action == "start":
                run_id = f"research_{len(stages) + 1}"
                stages[run_id] = "literature_survey"
                return json.dumps({"ok": True, "action": action,
                                   "result": {"id": run_id}})
            run_id = payload.get("run_id", "")
            if run_id not in stages:
                return json.dumps({"ok": False, "action": action,
                                   "error": f"unknown research run id: {run_id}"})
            if action == "literature":
                stages[run_id] = "completed"
            return json.dumps({"ok": True, "action": action,
                               "result": {"id": run_id, "stage": stages[run_id]}})

        harness.handlers["research_workflow"] = workflow
        harness.research_run_stage = stages.get
        return harness, stages, calls

    def test_start_id_is_injected_into_unambiguous_literature_call(self):
        h, stages, calls = self.make_harness([
            tool("start", {"problem_ref": "problem"}),
            tool("literature", {"difficulty": "easy"}),
            prose("verified result"),
        ])
        messages = [{"role": "user", "content": "research problem"}]
        h.runtime().run(messages, {})
        self.assertEqual(calls[1][1]["run_id"], "research_1")
        self.assertEqual(stages["research_1"], "completed")
        self.assertEqual(messages[-1]["content"][0].text, "verified result")

    def test_wrong_explicit_id_is_not_overridden_and_proof_is_blocked(self):
        h, _, calls = self.make_harness([
            tool("start", {"problem_ref": "problem"}),
            tool("literature", {"run_id": "research_wrong", "difficulty": "easy"}),
            prose(), prose(), prose(),
        ])
        messages = [{"role": "user", "content": "research problem"}]
        h.runtime().run(messages, {})
        self.assertEqual(calls[1][1]["run_id"], "research_wrong")
        assistant_text = [
            block.get("text", "") if isinstance(block, dict) else block.text
            for msg in messages if msg.get("role") == "assistant"
            for block in msg.get("content", [])
            if (block.get("type") if isinstance(block, dict) else block.type) == "text"
        ]
        self.assertNotIn("unverified proof", assistant_text)
        self.assertIn("Research workflow execution failure", assistant_text[-1])

    def test_start_tracks_returned_id_even_with_stale_input_id(self):
        h, stages, _ = self.make_harness([
            tool("start", {"problem_ref": "problem", "run_id": "research_stale"}),
            tool("literature", {"run_id": "research_1", "difficulty": "easy"}),
            prose("verified result"),
        ])
        messages = [{"role": "user", "content": "research problem"}]
        h.runtime().run(messages, {})
        self.assertEqual(stages["research_1"], "completed")
        self.assertEqual(messages[-1]["content"][0].text, "verified result")

    def test_method_result_id_is_not_tracked_as_research_run(self):
        h, stages, _ = self.make_harness([
            tool("start", {"problem_ref": "problem"}),
            tool("structural_verdict", {"run_id": "research_1", "method_id": "method_01"}),
            tool("literature", {"run_id": "research_1", "difficulty": "easy"}),
            prose("verified result"),
        ])
        original = h.handlers["research_workflow"]

        def workflow(action, payload=None):
            if action == "structural_verdict":
                return json.dumps({"ok": True, "action": action,
                                   "result": {"id": "method_01", "stage": "detailed_verify"}})
            return original(action, payload)

        h.handlers["research_workflow"] = workflow
        messages = [{"role": "user", "content": "research problem"}]
        h.runtime().run(messages, {})
        self.assertEqual(stages["research_1"], "completed")
        self.assertEqual(messages[-1]["content"][0].text, "verified result")

    def test_multiple_active_runs_do_not_guess_id(self):
        h, _, calls = self.make_harness([
            tool("start", {"problem_ref": "one"}),
            tool("start", {"problem_ref": "two"}),
            tool("literature", {"difficulty": "easy"}),
            prose(), prose(), prose(),
        ])
        messages = [{"role": "user", "content": "two problems"}]
        h.runtime().run(messages, {})
        self.assertEqual([action for action, _ in calls], ["start", "start"])
        errors = [
            item["content"] for msg in messages
            if msg.get("role") == "user" and isinstance(msg.get("content"), list)
            for item in msg["content"] if item.get("type") == "tool_result"
        ]
        self.assertTrue(any("ambiguous active research runs" in error for error in errors))

    def test_partial_prose_is_suppressed_after_workflow_failure(self):
        h, _, _ = self.make_harness([
            tool("start", {"problem_ref": "problem"}),
            FakeResponse([
                FakeBlock("text", text="premature proof"),
                FakeBlock("tool_use", name="research_workflow",
                          tool_input={"action": "literature", "payload": {
                              "run_id": "research_wrong", "difficulty": "easy",
                          }}),
            ]),
            prose(), prose(), prose(),
        ])
        messages = [{"role": "user", "content": "research problem"}]
        h.runtime().run(messages, {})
        visible = [
            block.get("text", "") if isinstance(block, dict) else block.text
            for msg in messages if msg.get("role") == "assistant"
            for block in msg.get("content", [])
            if (block.get("type") if isinstance(block, dict) else block.type) == "text"
        ]
        self.assertNotIn("premature proof", visible)
        self.assertIn("Research workflow execution failure", visible[-1])

    def test_scope_switch_cannot_hide_a_run_started_in_this_turn(self):
        h, _, _ = self.make_harness([
            tool("start", {"problem_ref": "problem"}),
            prose(), prose(), prose(),
        ])
        scopes = iter(["scope_one", "scope_two", "scope_two", "scope_two"])
        h.update_context = lambda context, messages: {
            "active_memory_scope": next(scopes)
        }
        messages = [{"role": "user", "content": "research problem"}]
        h.runtime().run(messages, {})
        self.assertIn(
            "Research workflow execution failure",
            messages[-1]["content"][0]["text"],
        )

    def test_retrieval_result_is_passed_to_evidence_recorder(self):
        h, _, _ = self.make_harness([
            tool("start", {"problem_ref": "problem"}),
            FakeResponse([FakeBlock("tool_use", name="mcp__docs__search",
                                    tool_input={"query": "bifurcation"})]),
            tool("literature", {"difficulty": "easy"}),
            prose("verified result"),
        ])
        recorded = []
        h.handlers["mcp__docs__search"] = lambda query: (
            "Arxiv_ID: 2203.12345\nTitle: Bifurcation\nSummary: fixture"
        )
        h.record_literature_tool_result = (
            lambda run_id, name, args, result: recorded.append((run_id, name, result))
        )
        h.runtime().run([{"role": "user", "content": "research problem"}], {})
        self.assertEqual(recorded[0][0:2], ("research_1", "mcp__docs__search"))


if __name__ == "__main__":
    unittest.main()
