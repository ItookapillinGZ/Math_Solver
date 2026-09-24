import os
import tempfile
import unittest
from pathlib import Path

from agent_runtime.memory import MemoryKind, MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.db"
        self.store = MemoryStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_working_memory_upserts_by_key(self):
        first = self.store.upsert_working("goal", "prove lemma", priority=80)
        second = self.store.upsert_working("goal", "prove theorem", priority=90)
        self.assertEqual(first.id, second.id)
        records = self.store.list_memories(kind=MemoryKind.WORKING)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].content, "prove theorem")
        self.assertEqual(records[0].priority, 90)

    def test_episodes_are_append_only(self):
        one = self.store.add_episode("finished step 1", source="test")
        two = self.store.add_episode("finished step 2", source="test")
        self.assertNotEqual(one.id, two.id)
        records = self.store.list_memories(kind="episodic")
        self.assertEqual(len(records), 2)

    def test_artifact_upserts_by_path(self):
        first = self.store.upsert_artifact("PROOF/main.tex", description="draft")
        second = self.store.upsert_artifact("PROOF/main.tex", description="reviewed proof")
        self.assertEqual(first.id, second.id)
        records = self.store.list_memories(kind="artifact")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].content, "reviewed proof")

    def test_keyword_search_is_deterministic_and_needs_no_embedding(self):
        self.store.upsert_working("goal", "prove endemic equilibrium uniqueness", priority=90)
        self.store.add_episode("checked Picone identity", priority=60)
        self.store.add_episode("reviewed worktree isolation", priority=20)
        results = self.store.search_text("Picone")
        self.assertEqual(len(results), 1)
        self.assertIn("Picone", results[0].content)

    def test_build_context_separates_memory_kinds(self):
        self.store.upsert_working("goal", "prove theorem")
        self.store.add_episode("completed scalar reduction")
        self.store.upsert_artifact("PROOF/result.tex", description="final derivation")
        text = self.store.build_context()
        self.assertIn("Working memory:", text)
        self.assertIn("Recent episodic memory:", text)
        self.assertIn("Relevant artifacts:", text)
        self.assertIn("goal: prove theorem", text)
        self.assertIn("PROOF/result.tex", text)

    def test_build_context_respects_character_budget(self):
        for index in range(20):
            self.store.add_episode("episode " + str(index) + " " + ("x" * 100))
        text = self.store.build_context(max_chars=300)
        self.assertLessEqual(len(text), 320)
        self.assertIn("truncated", text.lower())

    def test_legacy_memory_file_is_imported_only_once(self):
        legacy = Path(self.temp_dir.name) / "MEMORY.md"
        legacy.write_text("Important old proof note", encoding="utf-8")
        self.assertTrue(self.store.import_legacy_memory_file(legacy))
        self.assertFalse(self.store.import_legacy_memory_file(legacy))
        episodes = self.store.list_memories(kind="episodic")
        self.assertEqual(len(episodes), 1)
        self.assertIn("Important old proof note", episodes[0].content)

    def test_read_connections_release_database_file(self):
        self.store.upsert_working("goal", "test")
        self.store.list_memories()
        self.store.search_text("test")
        replacement = Path(self.temp_dir.name) / "renamed.db"
        os.replace(self.db_path, replacement)
        self.assertTrue(replacement.exists())


if __name__ == "__main__":
    unittest.main()
