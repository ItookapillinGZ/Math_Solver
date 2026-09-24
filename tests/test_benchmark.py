from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.run_benchmark import (
    evaluate_checklist,
    load_case,
    load_cases,
    load_expected,
    run_offline_case,
)


class BenchmarkTests(unittest.TestCase):
    def test_loads_four_cases_and_rejects_invalid_schema(self):
        pairs = load_cases()
        self.assertEqual(len(pairs), 4)
        self.assertEqual({case["id"] for case, _ in pairs},
                         {"case_01", "case_02", "case_03", "case_04"})
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "invalid.json"
            bad.write_text(json.dumps({"id": "bad", "title": "x", "difficulty": "easy",
                                       "offline_scenario": {"methods": []}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "problem is required"):
                load_case(bad)
            bad.write_text(json.dumps({"case_id": "case_01", "branch": "direct_proof"}),
                           encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "min_methods"):
                load_expected(bad, "case_01")

    def test_easy_offline_uses_no_api_or_network(self):
        case, expected = load_cases("case_01")[0]
        with tempfile.TemporaryDirectory() as temp:
            with patch("socket.create_connection", side_effect=AssertionError("network accessed")):
                with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
                    result = run_offline_case(case, expected, Path(temp))
            self.assertTrue(result["success"], result["checks"])
            self.assertEqual(result["methods_total"], 0)
            self.assertIsNone(result["model_calls"])
            self.assertIsNone(result["input_tokens"])
            self.assertIn("NOT A MATHEMATICAL PROOF",
                          Path(result["final_artifact"]).read_text(encoding="utf-8"))
            json.dumps(result)

    def test_hard_revision_loop_and_missing_gate_detection(self):
        case, expected = load_cases("case_04")[0]
        with tempfile.TemporaryDirectory() as temp:
            result = run_offline_case(case, expected, Path(temp))
            self.assertTrue(result["success"], result["checks"])
            self.assertEqual(result["structural_continue"], 1)
            self.assertEqual(result["proof_revisions"], 1)
            self.assertTrue(result["checks"]["taskstore_dependency_dag"])
            self.assertTrue(result["checks"]["regulator_revision_loop"])
            result_dir = Path(result["result_dir"])
            from agent_runtime.observability.store import TraceStore
            trace = TraceStore(result_dir / "traces.db")
            run_id = "benchmark_case_04"
            snapshot = trace.research_snapshot(run_id)
            events = trace.list_workflow_events(run_id)
            roles = ["Mathematical Decomposer"]
            kwargs = {
                "event_types": [event["event_type"] for event in events],
                "spawned_roles": roles,
                "dag_valid": True,
                "premature_summary_rejected": True,
                "detailed_gate_rejected": True,
            }
            missing_detailed = dict(result, detailed_done=0)
            checks = evaluate_checklist(missing_detailed, snapshot, expected, **kwargs)
            self.assertFalse(checks["detailed_verification"])
            missing_artifact = dict(result, final_artifact=str(result_dir / "missing.md"))
            checks = evaluate_checklist(missing_artifact, snapshot, expected, **kwargs)
            self.assertFalse(checks["final_artifact_exists"])
            json.dumps(result)


if __name__ == "__main__":
    unittest.main()

