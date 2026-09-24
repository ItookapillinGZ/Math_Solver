from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent_runtime.research import (
    ResearchToolRuntime, ResearchWorkflowCoordinator, ResearchWorkflowOrchestrator,
    ResearchWorkflowOrchestratorDependencies,
)
from agent_runtime.research.literature_search import (
    build_arxiv_search_query, build_arxiv_url, deduplicate_sources,
    normalize_crossref_item, rank_sources,
)


QUERY = "Crandall Rabinowitz bifurcation from simple eigenvalues"
CLASSIC = {
    "DOI": "10.1016/0022-1236(71)90015-2",
    "title": ["Bifurcation from Simple Eigenvalues"],
    "author": [{"given": "Michael G.", "family": "Crandall"},
               {"given": "Paul H.", "family": "Rabinowitz"}],
    "type": "journal-article", "published-print": {"date-parts": [[1971]]},
}


class Response:
    status_code = 200

    def __init__(self, *, content=b"", data=None, status_code=200):
        self.content, self.data, self.status_code = content, data, status_code

    def json(self):
        return self.data


def arxiv_feed(title="Bifurcation from Simple Eigenvalues", doi=""):
    return (f"<feed xmlns='http://www.w3.org/2005/Atom' xmlns:arxiv='http://arxiv.org/schemas/atom'>"
            f"<entry><id>https://arxiv.org/abs/2401.00001</id><title>{title}</title>"
            f"<summary>Local bifurcation at simple eigenvalues.</summary>"
            f"<author><name>A. Author</name></author>"
            f"{f'<arxiv:doi>{doi}</arxiv:doi>' if doi else ''}</entry></feed>").encode()


class LiteratureRetrievalQualityTests(unittest.TestCase):
    def test_phrase_aware_query_and_explicit_sort(self):
        built = build_arxiv_search_query(QUERY)
        self.assertIn('all:"Crandall Rabinowitz"', built)
        self.assertIn(" AND all:bifurcation", built)
        self.assertNotIn("all:Crandall Rabinowitz", built)
        params = parse_qs(urlparse(build_arxiv_url(QUERY)).query)
        self.assertEqual(params["search_query"], [built])
        self.assertEqual(params["sortBy"], ["relevance"])
        self.assertEqual(params["sortOrder"], ["descending"])
        self.assertEqual(params["max_results"], ["10"])

    def test_special_characters_and_whitespace_are_escaped(self):
        url = build_arxiv_url('  "Lyapunov  Schmidt"   elliptic & λ+u^3  ')
        params = parse_qs(urlparse(url).query)
        self.assertEqual(len(params), 5)
        self.assertIn('all:"Lyapunov Schmidt"', params["search_query"][0])
        self.assertIn("all:elliptic", params["search_query"][0])
        self.assertNotIn("&", params["search_query"][0])
        self.assertIn("%CE%BB", url)

    def test_crossref_normalization_has_real_metadata_only(self):
        source = normalize_crossref_item(CLASSIC, QUERY, "2026-01-01T00:00:00+00:00")
        self.assertEqual(source["source_id"], CLASSIC["DOI"].lower())
        self.assertEqual(source["doi"], CLASSIC["DOI"].lower())
        self.assertEqual(source["published_year"], 1971)
        self.assertEqual(source["authors"], ["Michael G. Crandall", "Paul H. Rabinowitz"])
        self.assertEqual(source["abstract"], "")

    def test_provider_failure_isolation_and_trace(self):
        def fake_get(url, **kwargs):
            if "arxiv.org" in url:
                raise OSError("arXiv offline")
            return Response(data={"message": {"items": [CLASSIC]}})

        with tempfile.TemporaryDirectory() as td:
            runtime = ResearchToolRuntime(Path(td), http_get=fake_get, sleep=lambda _: None)
            result = json.loads(runtime.search_literature(QUERY))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["provider_statuses"]["arxiv"]["status"], "failed")
        self.assertEqual(result["provider_statuses"]["crossref"]["status"], "success")
        self.assertEqual(result["sources"][0]["title"], "Bifurcation from Simple Eigenvalues")
        self.assertEqual(result["raw_result_count"], 1)
        events = []
        orch = ResearchWorkflowOrchestrator(ResearchWorkflowCoordinator(), ResearchWorkflowOrchestratorDependencies(
            create_task=lambda **kwargs: None, load_task=lambda task_id: None,
            spawn_teammate=lambda *args: "", send_message=lambda *args: "",
            terminal_print=lambda text: None,
            record_event=lambda kind, run_id, payload: events.append((kind, payload)),
        ))
        run = orch.start_run("problem")
        orch.record_literature_query_started(run.id, "search_literature", {"query": QUERY})
        orch.record_literature_tool_result(run.id, "search_literature", {"query": QUERY}, json.dumps(result))
        self.assertIn(("literature.query_started", {"query": QUERY, "providers": ["arxiv", "crossref"]}), events)
        self.assertEqual([kind for kind, _ in events].count("literature.provider_failed"), 1)
        self.assertEqual([kind for kind, _ in events].count("literature.provider_completed"), 1)
        completed = [payload for kind, payload in events if kind == "literature.query_completed"][0]
        self.assertEqual(completed["raw_result_count"], 1)
        self.assertEqual(completed["deduplicated_result_count"], 1)

    def test_multi_provider_merge_and_doi_dedup(self):
        def fake_get(url, **kwargs):
            if "arxiv.org" in url:
                return Response(content=arxiv_feed("An early version", CLASSIC["DOI"]))
            return Response(data={"message": {"items": [CLASSIC]}})
        with tempfile.TemporaryDirectory() as td:
            result = json.loads(ResearchToolRuntime(Path(td), http_get=fake_get, sleep=lambda _: None).search_literature(QUERY))
        self.assertEqual(result["raw_result_count"], 2)
        self.assertEqual(result["deduplicated_result_count"], 1)
        source = result["sources"][0]
        self.assertEqual(source["provider"], "crossref")
        self.assertEqual(source["providers"], ["arxiv", "crossref"])
        self.assertIn("2401.00001", source["source_ids"])
        self.assertIn(CLASSIC["DOI"].lower(), source["source_ids"])

    def test_normalized_title_dedup(self):
        sources = [
            {"provider": "arxiv", "source_type": "arxiv", "source_id": "1", "title": "Local Bifurcation: Simple Eigenvalues"},
            {"provider": "crossref", "source_type": "journal-article", "source_id": "10.1/x", "doi": "10.1/x", "title": "Local bifurcation — simple eigenvalues"},
        ]
        merged = deduplicate_sources(sources)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["providers"], ["arxiv", "crossref"])

    def test_fixed_fixture_ranking_is_deterministic(self):
        classic = normalize_crossref_item(CLASSIC, QUERY, "2026-01-01T00:00:00+00:00")
        irrelevant = [
            {"source_id": "a", "title": "Microwave Residual Surface Resistance of Superconductors", "authors": [], "abstract": ""},
            {"source_id": "b", "title": "Exploring the Kibble-Zurek mechanism in a secondary bifurcation", "authors": [], "abstract": ""},
            {"source_id": "c", "title": "The structure and evolution of confined tori near a Hamiltonian Hopf Bifurcation", "authors": [], "abstract": ""},
        ]
        ordered = rank_sources([*irrelevant, classic], QUERY)
        self.assertEqual(ordered[0]["title"], "Bifurcation from Simple Eigenvalues")
        self.assertEqual([s["source_id"] for s in ordered],
                         [s["source_id"] for s in rank_sources([*irrelevant, classic], QUERY)])

    def test_ranking_never_selects_and_unknown_source_is_rejected(self):
        source = normalize_crossref_item(CLASSIC, QUERY)
        orch = ResearchWorkflowOrchestrator(ResearchWorkflowCoordinator(), ResearchWorkflowOrchestratorDependencies(
            create_task=lambda **kwargs: None, load_task=lambda task_id: None,
            spawn_teammate=lambda *args: "", send_message=lambda *args: "",
            terminal_print=lambda text: None,
        ))
        run = orch.start_run("problem")
        orch.record_literature_tool_result(run.id, "search_literature", {"query": QUERY}, json.dumps({
            "query": QUERY, "provider": "builtin_literature", "status": "success", "sources": [source],
        }))
        self.assertEqual(run.selected_sources, [])
        result = json.loads(orch.dispatch("literature", {
            "run_id": run.id, "difficulty": "easy", "literature_notes": "Review",
            "selected_sources": [{"source_id": "not-retrieved", "relevance_note": "guess"}],
        }))
        self.assertFalse(result["ok"])
        self.assertIn("retrieved source_id", result["error"])


if __name__ == "__main__":
    unittest.main()
