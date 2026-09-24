import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.observability import (
    TracePricing,
    TraceStore,
    evaluate_research_run,
)
from agent_runtime.observability.viewer import TraceViewer
from agent_runtime.runtime.execution import (
    ExecutionTracker,
    RuntimeEventType,
    response_usage_payload,
)


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        pricing = TracePricing(
            input_per_mtok={"test-model": 2.0},
            output_per_mtok={"test-model": 10.0},
            cache_read_per_mtok={"test-model": 0.2},
            cache_write_per_mtok={"test-model": 2.5},
        )
        self.store = TraceStore(Path(self.tmp.name) / "traces.db", pricing=pricing)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pricing_can_be_loaded_from_json_without_hardcoded_vendor_prices(self):
        pricing = TracePricing.from_json(
            '{"model-x":{"input":1.5,"output":7.5,"cache_read":0.2}}'
        )
        self.assertEqual(pricing.input_per_mtok["model-x"], 1.5)
        self.assertEqual(pricing.output_per_mtok["model-x"], 7.5)
        self.assertEqual(TracePricing.from_json("not-json").input_per_mtok, {})

    def test_execution_tracker_persists_runtime_events(self):
        tracker = ExecutionTracker(event_sink=self.store.record_runtime_event)
        run, agent = tracker.start_run(agent_kind="lead", agent_name="lead", role="PI")
        tracker.emit(
            RuntimeEventType.MODEL_CALL_FINISHED,
            run_id=run.id,
            agent_id=agent.id,
            payload={
                "model": "test-model",
                "latency_ms": 125.5,
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 500,
                "retry_count": 2,
                "stop_reason": "end_turn",
            },
        )
        tool = tracker.start_tool_call(run_id=run.id, agent_id=agent.id, tool_name="read_file")
        tracker.finish_tool_call(tool.id)
        tracker.finish_run(run.id)

        summary = self.store.runtime_run_summary(run.id)
        self.assertEqual(summary["agent_name"], "lead")
        self.assertEqual(summary["model_calls"], 1)
        self.assertEqual(summary["input_tokens"], 1000)
        self.assertEqual(summary["output_tokens"], 200)
        self.assertEqual(summary["retry_count"], 2)
        self.assertEqual(summary["tool_calls"], 1)
        self.assertIsNotNone(summary["estimated_cost_usd"])

    def test_policy_and_task_transitions_are_queryable_in_summary(self):
        tracker = ExecutionTracker(event_sink=self.store.record_runtime_event)
        run, agent = tracker.start_run(agent_kind="lead", agent_name="lead")
        self.store.record_policy_decision(
            {
                "runtime_run_id": run.id,
                "agent_id": agent.id,
                "tool_name": "bash",
                "action": "deny",
                "reason": "test",
            }
        )
        self.store.record_task_transition(
            {
                "runtime_run_id": run.id,
                "task_id": "task_1",
                "transition": "created",
                "status": "pending",
                "owner": None,
            }
        )
        tracker.finish_run(run.id)
        summary = self.store.runtime_run_summary(run.id)
        self.assertEqual(summary["permission_decisions"], 1)
        self.assertEqual(summary["permission_denied"], 1)

    def test_workflow_snapshot_roundtrip_and_evaluation(self):
        run = {
            "id": "research_1",
            "problem_ref": "problem.tex",
            "stage": "completed",
            "difficulty": "hard",
            "literature_notes": "",
            "selected_method_ids": ["method_01"],
            "final_artifact": "proof.md",
            "methods": [
                {
                    "id": "method_01",
                    "name": "energy",
                    "rationale": "",
                    "teammate": "worker",
                    "stage": "completed",
                    "steps": [
                        {"id": "S1", "goal": "x", "depends_on": [], "status": "completed"},
                        {"id": "S2", "goal": "y", "depends_on": ["S1"], "status": "completed"},
                    ],
                    "structural_verdict": "done",
                    "detailed_verdict": "done",
                    "regulator_action": None,
                    "regulator_notes": "",
                    "revision_pending": False,
                    "proof_revision_count": 1,
                    "plan_revision_count": 2,
                    "rewrite_count": 0,
                }
            ],
        }
        self.store.save_workflow_snapshot("research_1", run, [])
        self.store.record_workflow_event("complete_summary", "research_1", {"research_stage": "completed"})
        snapshot = self.store.research_snapshot("research_1")
        self.assertEqual(snapshot["run"]["final_artifact"], "proof.md")
        evaluation = evaluate_research_run(self.store, "research_1")
        self.assertEqual(evaluation.methods_completed, 1)
        self.assertEqual(evaluation.proof_steps_completed, 2)
        self.assertEqual(evaluation.proof_revisions, 1)
        self.assertEqual(evaluation.plan_revisions, 2)

    def test_evaluation_counts_historical_verdicts_after_revision(self):
        run = {
            "id": "research_revised",
            "problem_ref": "problem.tex",
            "stage": "methods_active",
            "difficulty": "hard",
            "methods": [{
                "id": "method_01",
                "stage": "regulation",
                "steps": [],
                "structural_verdict": None,
                "detailed_verdict": None,
                "proof_revision_count": 1,
                "plan_revision_count": 0,
                "rewrite_count": 0,
            }],
        }
        self.store.save_workflow_snapshot("research_revised", run, [])
        self.store.record_workflow_event(
            "structural_verdict", "research_revised",
            {"method_id": "method_01", "verdict": "continue"},
        )
        self.store.record_workflow_event(
            "regulator", "research_revised",
            {"method_id": "method_01", "regulator_action": "revise_proof"},
        )
        evaluation = evaluate_research_run(self.store, "research_revised")
        self.assertEqual(evaluation.structural_continue, 1)
        self.assertEqual(evaluation.structural_done, 0)

    def test_trace_viewer_is_human_readable(self):
        self.store.save_workflow_snapshot(
            "research_2",
            {
                "id": "research_2",
                "problem_ref": "p.tex",
                "stage": "methods_active",
                "difficulty": "medium",
                "literature_notes": "",
                "methods": [],
                "selected_method_ids": [],
                "final_artifact": None,
            },
            [],
        )
        text = TraceViewer(self.store).render_research_run("research_2")
        self.assertIn("Research Run: research_2", text)
        self.assertIn("Difficulty: medium", text)

    def test_response_usage_payload_handles_sdk_like_object(self):
        response = SimpleNamespace(
            model="test-model",
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=12,
                output_tokens=7,
                cache_creation_input_tokens=3,
                cache_read_input_tokens=4,
            ),
        )
        payload = response_usage_payload(response)
        self.assertEqual(payload["input_tokens"], 12)
        self.assertEqual(payload["cache_read_input_tokens"], 4)

    def test_workflow_snapshot_upsert_is_latest(self):
        base = {
            "id": "research_3",
            "problem_ref": "p.tex",
            "stage": "literature_survey",
            "difficulty": None,
            "literature_notes": "",
            "methods": [],
            "selected_method_ids": [],
            "final_artifact": None,
        }
        self.store.save_workflow_snapshot("research_3", base, [])
        newer = dict(base, stage="direct_proof", difficulty="easy")
        self.store.save_workflow_snapshot("research_3", newer, [])
        rows = self.store.load_workflow_snapshots()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run"]["stage"], "direct_proof")


if __name__ == "__main__":
    unittest.main()
