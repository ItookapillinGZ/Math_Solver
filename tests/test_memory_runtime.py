from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime.memory import (
    MemoryRuntime,
    MemoryRuntimeConfig,
    MemoryRuntimeDependencies,
    MemoryStore,
)


class MemoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root / "memory.db")
        self.logs: list[str] = []

        def resolve_path(raw: str) -> Path:
            target = (self.root / raw).resolve()
            target.relative_to(self.root.resolve())
            return target

        self.runtime = MemoryRuntime(
            MemoryRuntimeConfig(
                workspace=self.root,
                legacy_memory_path=self.root / ".memory" / "MEMORY.md",
                context_max_chars=2000,
            ),
            MemoryRuntimeDependencies(
                store=self.store,
                resolve_path=resolve_path,
                terminal_print=self.logs.append,
            ),
        )
        self.runtime.initialize_session(scope_id="scope-a", label="Problem A")

    def tearDown(self):
        self.tmp.cleanup()

    def test_initialize_session_sets_active_scope(self):
        self.assertEqual(self.runtime.active_scope_id, "scope-a")
        scope_ids = [s.id for s in self.store.list_scopes(include_archived=True)]
        self.assertIn("scope-a", scope_ids)

    def test_working_memory_is_scoped_and_current_includes_global(self):
        current = json.loads(self.runtime.set_working("strategy", "Picone", 90))
        self.assertEqual(current["scope_id"], "scope-a")
        global_record = self.store.upsert_working(
            "notation", "phi_1 is principal eigenfunction", scope_id="global", priority=80
        )
        payload = json.loads(self.runtime.list_memories(kind="working", scope="current"))
        ids = {item["id"] for item in payload}
        self.assertIn(current["id"], ids)
        self.assertIn(global_record.id, ids)

    def test_search_defaults_to_all_scopes(self):
        self.runtime.add_episode("Picone attempt failed")
        self.runtime.start_scope("Problem B")
        payload = json.loads(self.runtime.search_memories("Picone"))
        self.assertEqual(len(payload), 1)
        self.assertIn("Picone", payload[0]["content"])

    def test_start_scope_archives_previous_problem_scope(self):
        response = json.loads(self.runtime.start_scope("Problem B"))
        self.assertEqual(response["previous_scope"], "scope-a")
        self.assertEqual(self.runtime.active_scope_id, response["active_scope"]["id"])
        old = self.store.get_scope("scope-a")
        self.assertIsNotNone(old)
        self.assertTrue(old.archived)

    def test_switch_scope_reactivates_target_and_archives_current(self):
        created = json.loads(self.runtime.start_scope("Problem B"))
        scope_b = created["active_scope"]["id"]
        response = json.loads(self.runtime.switch_scope("scope-a"))
        self.assertEqual(response["active_scope"]["id"], "scope-a")
        self.assertEqual(self.runtime.active_scope_id, "scope-a")
        self.assertTrue(self.store.get_scope(scope_b).archived)
        self.assertFalse(self.store.get_scope("scope-a").archived)

    def test_artifact_path_is_workspace_relative_and_escape_is_rejected(self):
        inside = self.root / "PROOF" / "main.tex"
        inside.parent.mkdir()
        inside.write_text("proof", encoding="utf-8")
        record = json.loads(self.runtime.add_artifact("PROOF/main.tex", "proof"))
        self.assertEqual(record["metadata"]["path"], "PROOF/main.tex")
        error = self.runtime.add_artifact("../outside.txt")
        self.assertTrue(error.startswith("Error:"))

    def test_promote_global_preserves_historical_source(self):
        record = json.loads(self.runtime.set_working("constraint", "Neumann BC"))
        promoted = json.loads(self.runtime.promote_global(record["id"]))
        self.assertEqual(promoted["scope_id"], "global")
        self.assertEqual(promoted["content"], "Neumann BC")

    def test_build_context_only_uses_current_plus_global(self):
        self.runtime.set_working("a", "problem A only")
        self.store.upsert_working("global-note", "global stable fact", scope_id="global")
        self.runtime.start_scope("Problem B")
        self.runtime.set_working("b", "problem B only")
        context = self.runtime.build_context()
        self.assertIn("problem B only", context)
        self.assertIn("global stable fact", context)
        self.assertNotIn("problem A only", context)

    def test_legacy_memory_migration_runs_once_during_initialization(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / ".memory" / "MEMORY.md"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("legacy theorem note", encoding="utf-8")
            store = MemoryStore(root / "memory.db")
            logs: list[str] = []
            runtime = MemoryRuntime(
                MemoryRuntimeConfig(root, legacy),
                MemoryRuntimeDependencies(
                    store=store,
                    resolve_path=lambda p: (root / p).resolve(),
                    terminal_print=logs.append,
                ),
            )
            runtime.initialize_session(scope_id="first", label="First")
            runtime2 = MemoryRuntime(
                MemoryRuntimeConfig(root, legacy),
                MemoryRuntimeDependencies(
                    store=store,
                    resolve_path=lambda p: (root / p).resolve(),
                    terminal_print=logs.append,
                ),
            )
            runtime2.initialize_session(scope_id="second", label="Second")
            found = store.search_text("legacy theorem note", scope_ids=None)
            self.assertEqual(len(found), 1)
            self.assertEqual(sum("migrated legacy" in line for line in logs), 1)


if __name__ == "__main__":
    unittest.main()
