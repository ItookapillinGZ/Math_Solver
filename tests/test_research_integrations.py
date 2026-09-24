from __future__ import annotations

import gzip
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from agent_runtime.research import MockMCPRuntime, ResearchToolRuntime, SkillCatalog


class FakeResponse:
    def __init__(self, *, status_code=200, text="", content=None):
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")


class SkillCatalogTests(unittest.TestCase):
    def test_scan_list_and_load_skill(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = root / "proof-review"
            skill_dir.mkdir()
            raw = (
                "---\n"
                "name: proof-review\n"
                "description: Review a mathematical proof\n"
                "---\n"
                "# Instructions\nCheck every estimate.\n"
            )
            (skill_dir / "SKILL.md").write_text(raw)
            catalog = SkillCatalog(root)
            self.assertIn("proof-review", catalog.registry)
            self.assertIn("Review a mathematical proof", catalog.list_text())
            self.assertEqual(catalog.load("proof-review"), raw)

    def test_missing_skill_reports_available_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "known"
            d.mkdir()
            (d / "SKILL.md").write_text("# Known\n")
            catalog = SkillCatalog(root)
            result = catalog.load("missing")
            self.assertIn("Skill not found: missing", result)
            self.assertIn("known", result)


class ResearchToolRuntimeTests(unittest.TestCase):
    def test_arxiv_search_parses_feed(self):
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
  <entry>
    <id>https://arxiv.org/abs/2601.12345v2</id>
    <title>  A PDE   Paper </title>
    <summary> A useful summary. </summary>
  </entry>
</feed>"""
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(text=xml)

        with tempfile.TemporaryDirectory() as td:
            runtime = ResearchToolRuntime(
                Path(td),
                terminal_print=lambda *_: None,
                http_get=fake_get,
                sleep=lambda *_: None,
                random_uniform=lambda *_: 0.0,
                random_choice=lambda xs: xs[0],
            )
            result = runtime.search_arxiv("cross diffusion")
            self.assertIn("Arxiv_ID: 2601.12345v2", result)
            self.assertIn("Title: A PDE Paper", result)
            self.assertIn("Summary: A useful summary.", result)
            self.assertIn("all%3Across%20AND%20all%3Adiffusion", calls[0][0])

    def test_arxiv_search_rate_limit_degrades_cleanly(self):
        def fake_get(url, **kwargs):
            return FakeResponse(status_code=503, text="Rate exceeded")

        with tempfile.TemporaryDirectory() as td:
            runtime = ResearchToolRuntime(
                Path(td),
                terminal_print=lambda *_: None,
                http_get=fake_get,
                sleep=lambda *_: None,
                random_uniform=lambda *_: 0.0,
                random_choice=lambda xs: xs[0],
            )
            result = runtime.search_arxiv("sis")
            self.assertIn("Rate limit exceeded", result)
            self.assertIn("view_latex_theorem", result)

    def test_download_single_gzip_source(self):
        title_xml = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
  <entry><title>Test / PDE: Paper</title></entry>
</feed>"""
        latex = b"\\documentclass{article}\n\\begin{document}Hi\\end{document}\n"
        gz = gzip.compress(latex)
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if "api/query" in url:
                return FakeResponse(text=title_xml)
            return FakeResponse(content=gz)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = ResearchToolRuntime(
                root,
                terminal_print=lambda *_: None,
                http_get=fake_get,
            )
            result = runtime.download_arxiv_source("2601.12345v2")
            self.assertIn("Single LaTeX file deflated successfully.", result)
            folders = list((root / "Conference").iterdir())
            self.assertEqual(len(folders), 1)
            self.assertNotIn("/", folders[0].name)
            tex = folders[0] / "main_2601.12345.tex"
            self.assertEqual(tex.read_bytes(), latex)
            self.assertEqual(len(calls), 2)


    def test_download_multi_file_tar_uses_safe_extraction(self):
        title_xml = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
  <entry><title>Safe Tar Paper</title></entry>
</feed>"""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            data = b"\\documentclass{article}\n"
            info = tarfile.TarInfo("src/main.tex")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        payload = buffer.getvalue()

        def fake_get(url, **kwargs):
            if "api/query" in url:
                return FakeResponse(text=title_xml)
            return FakeResponse(content=payload)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = ResearchToolRuntime(
                root,
                terminal_print=lambda *_: None,
                http_get=fake_get,
            )
            result = runtime.download_arxiv_source("2601.22222")
            self.assertIn("Multi-file project extracted successfully.", result)
            paper = next((root / "Conference").iterdir())
            self.assertEqual((paper / "src" / "main.tex").read_bytes(), data)

    def test_unsafe_tar_fails_closed_instead_of_gzip_or_raw_fallback(self):
        title_xml = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
  <entry><title>Unsafe Tar Paper</title></entry>
</feed>"""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            data = b"owned"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        payload = buffer.getvalue()

        def fake_get(url, **kwargs):
            if "api/query" in url:
                return FakeResponse(text=title_xml)
            return FakeResponse(content=payload)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = ResearchToolRuntime(
                root,
                terminal_print=lambda *_: None,
                http_get=fake_get,
            )
            result = runtime.download_arxiv_source("2601.33333")
            self.assertIn("Unsafe source archive rejected", result)
            self.assertFalse((root / "escape.txt").exists())
            paper = next((root / "Conference").iterdir())
            self.assertFalse((paper / "main_2601.33333.tex").exists())
            self.assertFalse((paper / "source_archive.tmp").exists())

    def test_download_filesystem_path_does_not_trust_raw_arxiv_id(self):
        title_xml = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
  <entry><title>Path Test</title></entry>
</feed>"""
        latex = b"\\documentclass{article}\n"
        gz = gzip.compress(latex)

        def fake_get(url, **kwargs):
            if "api/query" in url:
                return FakeResponse(text=title_xml)
            return FakeResponse(content=gz)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = ResearchToolRuntime(
                root, terminal_print=lambda *_: None, http_get=fake_get
            )
            result = runtime.download_arxiv_source("../2601.44444v1")
            self.assertIn("Successfully saved to", result)
            conference = (root / "Conference").resolve()
            paper = next(conference.iterdir()).resolve()
            self.assertTrue(paper.is_relative_to(conference))
            self.assertFalse((root.parent / "2601.44444").exists())

    def test_view_latex_theorem_fuzzy_arxiv_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper = root / "Conference" / "Example (2601.12345)"
            paper.mkdir(parents=True)
            tex = paper / "main_2601.12345.tex"
            tex.write_text("line one\nline two\nline three\nline four\n")
            runtime = ResearchToolRuntime(root, terminal_print=lambda *_: None)
            result = runtime.view_latex_theorem(
                "something/2601.12345/unknown.tex", start_line=2, num_lines=2
            )
            self.assertIn("2: line two", result)
            self.assertIn("3: line three", result)
            self.assertNotIn("4: line four", result)


class MockMCPRuntimeTests(unittest.TestCase):
    def test_connect_docs_and_dynamic_tool_pool(self):
        logs = []
        runtime = MockMCPRuntime(
            arxiv_search=lambda query, **_: f"found:{query}",
            terminal_print=logs.append,
        )
        result = runtime.connect("docs")
        self.assertIn("Connected to MCP server 'docs'", result)
        self.assertEqual(runtime.list_servers(), ["docs"])

        tools, handlers = runtime.assemble_tool_pool(
            registry_tools=[{"name": "read_file"}],
            builtin_tools=[{"name": "compact"}],
            registry_handlers={"read_file": lambda: "read"},
        )
        names = {tool["name"] for tool in tools}
        self.assertIn("mcp__docs__search", names)
        self.assertIn("mcp__docs__get_version", names)
        self.assertEqual(
            handlers["mcp__docs__search"](query="reaction diffusion"),
            "found:reaction diffusion",
        )
        self.assertTrue(any("[mcp] connected" in line for line in logs))

    def test_connect_is_idempotent_and_unknown_server_is_reported(self):
        runtime = MockMCPRuntime(arxiv_search=lambda query, **_: query)
        first = runtime.connect("deploy")
        second = runtime.connect("deploy")
        unknown = runtime.connect("missing")
        self.assertIn("Connected to MCP server 'deploy'", first)
        self.assertEqual(second, "MCP server 'deploy' already connected")
        self.assertIn("Unknown server 'missing'", unknown)

    def test_deploy_handler_contract_is_preserved(self):
        runtime = MockMCPRuntime(arxiv_search=lambda query, **_: query)
        runtime.connect("deploy")
        _, handlers = runtime.assemble_tool_pool([], [], {})
        self.assertEqual(
            handlers["mcp__deploy__status"](service="solver"),
            "[deploy] solver: running (v1.4.2)",
        )
        self.assertEqual(
            handlers["mcp__deploy__trigger"](service="solver"),
            "[deploy] Triggered: solver",
        )

    def test_normalize_mcp_name(self):
        self.assertEqual(MockMCPRuntime.normalize_name("docs server/v2"), "docs_server_v2")


if __name__ == "__main__":
    unittest.main()
