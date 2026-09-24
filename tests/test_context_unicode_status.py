from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime.context import ContextRuntime, ContextRuntimeConfig, ContextRuntimeDependencies
from agent_runtime.memory import MemoryStore
from agent_runtime.research import (
    ResearchWorkflowCoordinator, ResearchWorkflowOrchestrator,
    ResearchWorkflowOrchestratorDependencies,
)


class ContextUnicodeStatusTests(unittest.TestCase):
    def test_large_unicode_tool_output_is_persisted_as_utf8_and_rewrites_partial_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = MemoryStore(root / "memory.db")
            runtime = ContextRuntime(
                ContextRuntimeConfig(
                    transcript_dir=root / "transcripts",
                    tool_results_dir=root / "tool_results",
                    persist_threshold=10,
                    token_limit=1000,
                ),
                ContextRuntimeDependencies(
                    summarize_checkpoint=lambda prompt: "{}",
                    memory_store=store,
                    get_active_scope_id=lambda: "scope_test",
                ),
            )
            output = "PDE on ℝⁿ: λ₁, Ω, ∫Ω φ₁⁴ dx\n" * 20
            path = root / "tool_results" / "tool1.txt"
            path.parent.mkdir()
            path.write_bytes(b"partial")
            reference = runtime.persist_large_output("tool1", output)
            self.assertIn("<persisted-output>", reference)
            self.assertEqual(path.read_text(encoding="utf-8"), output)

    def test_status_is_compact_without_losing_persisted_retrieval_evidence(self):
        orch = ResearchWorkflowOrchestrator(
            ResearchWorkflowCoordinator(),
            ResearchWorkflowOrchestratorDependencies(
                create_task=lambda **kwargs: None,
                load_task=lambda task_id: None,
                spawn_teammate=lambda *args: "",
                send_message=lambda *args: "",
                terminal_print=lambda text: None,
            ),
        )
        run = orch.start_run("Bifurcation on ℝⁿ")
        sources = [{
            "source_id": f"10.1234/paper-{i}", "source_type": "journal-article",
            "title": f"Semilinear elliptic bifurcation paper {i}",
            "provider": "crossref", "providers": ["crossref"],
            "doi": f"10.1234/paper-{i}", "published_year": 1971,
            "authors": ["A. Author"], "abstract": "ℝ" * 2000,
        } for i in range(80)]
        run.retrieved_sources = sources
        run.literature_sources = list(sources)
        run.literature_retrievals = [{
            "query": "elliptic bifurcation", "provider": "builtin_literature",
            "status": "success", "result_count": 80,
            "sources": sources, "latency_ms": 10,
        }]
        full_snapshot = run.to_dict()
        status = orch.status(run.id)
        compact = status["run"]
        self.assertEqual(compact["retrieved_source_count"], 80)
        self.assertEqual(len(compact["retrieved_sources"]), 80)
        self.assertEqual(compact["retrieved_sources"][0]["source_id"], sources[0]["source_id"])
        self.assertNotIn("abstract", compact["retrieved_sources"][0])
        self.assertNotIn("sources", compact["literature_retrievals"][0])
        self.assertNotIn("literature_sources", compact)
        self.assertEqual(full_snapshot["literature_retrievals"][0]["sources"], sources)
        self.assertLess(len(json.dumps(status, ensure_ascii=False)), 25_000)
        self.assertGreater(len(json.dumps(full_snapshot, ensure_ascii=False)), 200_000)


if __name__ == "__main__":
    unittest.main()
