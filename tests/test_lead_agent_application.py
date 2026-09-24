import unittest
from datetime import datetime
from pathlib import Path

from agent_runtime.application import LeadAgentApplication, LeadAgentDependencies


class LeadAgentApplicationTests(unittest.TestCase):
    def make_app(self, state=None):
        state = state or {
            "memory": "working: prove lemma",
            "scope": "scope_math_1",
            "mcp": ["docs"],
            "teammates": ["worker-a"],
            "skills": "- proof-checker: verifies derivations",
        }
        deps = LeadAgentDependencies(
            workspace=Path("C:/workspace/math-solver"),
            list_skills=lambda: state["skills"],
            build_memory_context=lambda: state["memory"],
            get_active_memory_scope=lambda: state["scope"],
            list_mcp_servers=lambda: state["mcp"],
            list_active_teammates=lambda: state["teammates"],
            now=lambda: datetime(2026, 9, 18, 10, 30, 45),
        )
        return LeadAgentApplication(deps), state

    def test_build_context_collects_live_domain_state(self):
        app, _ = self.make_app()
        context = app.build_context([])
        self.assertEqual(context["memories"], "working: prove lemma")
        self.assertEqual(context["active_memory_scope"], "scope_math_1")
        self.assertEqual(context["connected_mcp"], ["docs"])
        self.assertEqual(context["active_teammates"], ["worker-a"])

    def test_build_context_reads_dependencies_live_each_turn(self):
        app, state = self.make_app()
        self.assertEqual(app.build_context([])["active_memory_scope"], "scope_math_1")
        state["scope"] = "scope_math_2"
        state["memory"] = "working: new problem"
        refreshed = app.build_context([])
        self.assertEqual(refreshed["active_memory_scope"], "scope_math_2")
        self.assertEqual(refreshed["memories"], "working: new problem")

    def test_system_prompt_contains_research_identity_and_workspace(self):
        app, _ = self.make_app()
        prompt = app.build_system_prompt(app.build_context([]))
        self.assertIn("Lead Principal Investigator", prompt)
        self.assertIn(f"Working directory: {Path('C:/workspace/math-solver')}", prompt)
        self.assertIn("2026-09-18T10:30:45", prompt)

    def test_system_prompt_contains_skills_memory_scope_and_mcp(self):
        app, _ = self.make_app()
        prompt = app.build_system_prompt(app.build_context([]))
        self.assertIn("proof-checker", prompt)
        self.assertIn("Active memory scope: scope_math_1", prompt)
        self.assertIn("Relevant memories:\nworking: prove lemma", prompt)
        self.assertIn("Connected MCP servers: docs", prompt)

    def test_system_prompt_omits_empty_memory_and_mcp_sections(self):
        app, state = self.make_app()
        state["memory"] = ""
        state["mcp"] = []
        prompt = app.build_system_prompt(app.build_context([]))
        self.assertNotIn("Relevant memories:\n", prompt)
        self.assertNotIn("Connected MCP servers:", prompt)

    def test_memory_scope_rule_is_always_present(self):
        app, _ = self.make_app()
        prompt = app.build_system_prompt({})
        self.assertIn("problem-specific memories belong to the active scope", prompt)
        self.assertIn("memory_start_scope", prompt)

    def test_build_system_prompt_does_not_mutate_context(self):
        app, _ = self.make_app()
        context = app.build_context([])
        snapshot = dict(context)
        app.build_system_prompt(context)
        self.assertEqual(context, snapshot)


if __name__ == "__main__":
    unittest.main()
