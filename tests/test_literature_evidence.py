from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime.research import (
    ResearchToolRuntime,
    ResearchWorkflowCoordinator,
    ResearchWorkflowOrchestrator,
    ResearchWorkflowOrchestratorDependencies,
)


SOURCE = {
    "query": "bifurcation", "provider": "builtin_arxiv",
    "source_type": "arxiv", "source_id": "2203.12345",
    "title": "Simple eigenvalue bifurcation", "authors": ["A. Author"],
    "url": "https://arxiv.org/abs/2203.12345",
    "abstract": "A theorem", "retrieved_at": "2026-01-01T00:00:00+00:00",
}


class LiteratureEvidenceTests(unittest.TestCase):
    def make_orchestrator(self, *, offline=False):
        events = []
        orchestrator = ResearchWorkflowOrchestrator(
            ResearchWorkflowCoordinator(),
            ResearchWorkflowOrchestratorDependencies(
                create_task=lambda **kwargs: None,
                load_task=lambda task_id: None,
                spawn_teammate=lambda *args: "",
                send_message=lambda *args: "",
                terminal_print=lambda text: None,
                record_event=lambda kind, run_id, payload: events.append((kind, run_id, payload)),
                allow_offline_literature_fixture=offline,
            ),
        )
        return orchestrator, events

    def record(self, orch, run_id, query="bifurcation", sources=None, status="success"):
        return orch.record_literature_tool_result(
            run_id, "search_literature", {"query": query},
            json.dumps({
                "query": query, "provider": "builtin_arxiv", "status": status,
                "sources": sources if sources is not None else [SOURCE],
                "latency_ms": 12.5, "error_type": "network_error" if status == "failed" else "",
                "message": "offline" if status == "failed" else "",
            }),
        )

    def test_arxiv_normalization_and_retry_are_offline(self):
        feed = b"""<?xml version='1.0'?>
<feed xmlns='http://www.w3.org/2005/Atom'><entry>
<id>https://arxiv.org/abs/2203.12345</id><title> Simple eigenvalue bifurcation </title>
<summary>A theorem</summary><author><name>A. Author</name></author>
</entry></feed>"""
        calls = []
        class Response:
            status_code = 200
            content = feed
        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return Response()
        with tempfile.TemporaryDirectory() as td:
            runtime = ResearchToolRuntime(Path(td), http_get=fake_get, sleep=lambda _: None)
            result = json.loads(runtime.search_literature("bifurcation"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["sources"][0]["authors"], ["A. Author"])
        self.assertEqual(result["sources"][0]["source_id"], "2203.12345")
        self.assertEqual(calls[0][1]["timeout"], 4)
        self.assertIn("User-Agent", calls[0][1]["headers"])

    def test_arxiv_network_retry_is_finite(self):
        attempts = []
        def failing_get(url, **kwargs):
            attempts.append(kwargs["timeout"])
            raise OSError("offline")
        with tempfile.TemporaryDirectory() as td:
            runtime = ResearchToolRuntime(
                Path(td), http_get=failing_get, sleep=lambda _: None,
                literature_providers=("arxiv",),
            )
            result = json.loads(runtime.search_literature("bifurcation"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "network_error")
        self.assertEqual(attempts, [4, 4, 4])

    def test_arxiv_parse_failure_is_reported(self):
        class Response:
            status_code = 200
            content = b"<broken"
        with tempfile.TemporaryDirectory() as td:
            runtime = ResearchToolRuntime(
                Path(td), http_get=lambda *args, **kwargs: Response(),
                sleep=lambda _: None, literature_providers=("arxiv",),
            )
            result = json.loads(runtime.search_literature("bifurcation"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "parse_error")

    def test_multiple_queries_deduplicate_retrieved_but_not_select_automatically(self):
        orch, events = self.make_orchestrator()
        run = orch.start_run("problem")
        self.record(orch, run.id)
        self.record(orch, run.id, query="simple eigenvalue")
        self.assertEqual(len(run.literature_retrievals), 2)
        self.assertEqual(len(run.retrieved_sources), 1)
        self.assertEqual(run.selected_sources, [])
        self.assertEqual([x["query"] for x in run.literature_retrievals], ["bifurcation", "simple eigenvalue"])
        self.assertEqual(run.literature_retrievals[0]["result_count"], 1)
        self.assertEqual(run.literature_retrievals[0]["latency_ms"], 12.5)
        self.assertEqual([e[0] for e in events].count("literature.query_completed"), 2)

    def test_completion_requires_selected_source_and_notes(self):
        orch, events = self.make_orchestrator()
        run = orch.start_run("problem")
        self.record(orch, run.id)
        result = json.loads(orch.dispatch("literature", {
            "run_id": run.id, "difficulty": "easy", "literature_notes": "Relevant survey",
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(run.literature_status, "failed")
        self.assertEqual(run.stage.value, "failed")
        self.assertIn("literature.failed", [e[0] for e in events])

    def test_successful_selection_and_method_source_traceability(self):
        orch, events = self.make_orchestrator()
        run = orch.start_run("problem")
        self.record(orch, run.id)
        result = json.loads(orch.dispatch("literature", {
            "run_id": run.id, "difficulty": "hard", "auto_decompose": False,
            "literature_notes": "The source addresses a simple eigenvalue.",
            "selected_sources": [{"source_id": "2203.12345", "relevance_note": "Directly related"}],
            "methods": [
                {"name": "Crandall-Rabinowitz", "origin": "literature", "source_ids": ["2203.12345"]},
                {"name": "Direct expansion", "origin": "model_reasoning", "source_ids": []},
            ],
        }))
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.literature_status, "completed")
        self.assertEqual(run.selected_sources[0]["title"], SOURCE["title"])
        self.assertEqual(run.methods[0].source_ids, ("2203.12345",))
        self.assertEqual(run.methods[1].origin, "model_reasoning")
        self.assertIn("literature.source_selected", [e[0] for e in events])
        self.assertIn("literature.completed", [e[0] for e in events])

    def test_network_failure_degrades_and_allows_direct_proof_without_citations(self):
        orch, events = self.make_orchestrator()
        run = orch.start_run("problem")
        self.record(orch, run.id, sources=[], status="failed")
        result = json.loads(orch.dispatch("literature", {
            "run_id": run.id, "difficulty": "easy", "literature_status": "degraded",
            "literature_notes": "arXiv unavailable",
        }))
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.stage.value, "direct_proof")
        self.assertEqual(run.selected_sources, [])
        self.assertIn("literature.degraded", [e[0] for e in events])
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td) / "proof.md"
            artifact.write_text("# Proof\n", encoding="utf-8")
            completed = json.loads(orch.dispatch("complete_direct_proof", {
                "run_id": run.id, "artifact_path": str(artifact),
            }))
            self.assertTrue(completed["ok"], completed)
            content = artifact.read_text(encoding="utf-8")
            self.assertIn("Literature retrieval unavailable / degraded", content)
            self.assertIn("No literature citations were verified", content)
            self.assertNotIn("https://arxiv.org/abs/", content)

    def test_zero_results_can_degrade(self):
        orch, _ = self.make_orchestrator()
        run = orch.start_run("problem")
        self.record(orch, run.id, sources=[], status="no_results")
        result = json.loads(orch.dispatch("literature", {
            "run_id": run.id, "difficulty": "easy", "literature_status": "degraded",
        }))
        self.assertTrue(result["ok"], result)

    def test_degraded_requires_actual_tool_event(self):
        orch, _ = self.make_orchestrator()
        run = orch.start_run("problem")
        result = json.loads(orch.dispatch("literature", {
            "run_id": run.id, "difficulty": "easy", "literature_status": "degraded",
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(run.stage.value, "failed")

    def test_mcp_connection_is_not_retrieval_evidence(self):
        orch, _ = self.make_orchestrator()
        run = orch.start_run("problem")
        evidence = orch.record_literature_tool_result(
            run.id, "connect_mcp", {"name": "docs"},
            "Connected to MCP server 'docs'",
        )
        self.assertIsNone(evidence)
        self.assertEqual(run.literature_retrievals, [])

    def test_external_mcp_structured_result_is_normalized(self):
        orch, _ = self.make_orchestrator()
        run = orch.start_run("problem")
        result = orch.record_literature_tool_result(
            run.id, "mcp__docs__search", {"query": "bifurcation"},
            json.dumps({"sources": [SOURCE]}),
        )
        self.assertEqual(result["provider"], "external_mcp:docs")
        self.assertEqual(result["status"], "success")

    def test_wrong_explicit_run_id_is_rejected(self):
        orch, _ = self.make_orchestrator()
        orch.start_run("problem")
        result = json.loads(orch.dispatch("literature", {
            "run_id": "research_wrong", "difficulty": "easy",
        }))
        self.assertFalse(result["ok"])
        self.assertIn("unknown research run id", result["error"])

    def test_offline_fixture_is_explicit(self):
        offline, _ = self.make_orchestrator(offline=True)
        run = offline.start_run("problem")
        result = json.loads(offline.dispatch("literature", {
            "run_id": run.id, "difficulty": "easy", "offline_fixture": True,
        }))
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.literature_status, "offline_fixture")


if __name__ == "__main__":
    unittest.main()
