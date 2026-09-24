import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_runtime.memory import MemoryKind, MemoryStore


class MemoryScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "memory.db"
        self.store = MemoryStore(self.db_path)
        self.scope_a = self.store.create_scope("Problem A", scope_id="scope_a")
        self.scope_b = self.store.create_scope("Problem B", scope_id="scope_b")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_same_working_key_can_exist_in_separate_scopes(self):
        a = self.store.upsert_working(
            "proof_strategy", "Picone identity", scope_id="scope_a"
        )
        b = self.store.upsert_working(
            "proof_strategy", "energy estimate", scope_id="scope_b"
        )
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(a.scope_id, "scope_a")
        self.assertEqual(b.scope_id, "scope_b")

    def test_build_context_excludes_unrelated_problem_scope(self):
        self.store.upsert_working(
            "proof_strategy", "Picone identity", scope_id="scope_a"
        )
        self.store.upsert_working(
            "proof_strategy", "energy estimate", scope_id="scope_b"
        )
        context = self.store.build_context(scope_id="scope_b")
        self.assertIn("energy estimate", context)
        self.assertNotIn("Picone identity", context)

    def test_global_memory_is_visible_across_problem_scopes(self):
        local = self.store.upsert_working(
            "notation", "Use phi_1 for the principal eigenfunction", scope_id="scope_a"
        )
        promoted = self.store.promote_to_global(local.id)
        self.assertEqual(promoted.scope_id, "global")
        context = self.store.build_context(scope_id="scope_b")
        self.assertIn("phi_1", context)

    def test_problem_specific_strategy_is_not_global_until_promoted(self):
        local = self.store.upsert_working(
            "proof_strategy", "Use a ground-state transform", scope_id="scope_a"
        )
        before = self.store.build_context(scope_id="scope_b")
        self.assertNotIn("ground-state transform", before)
        self.store.promote_to_global(local.id)
        after = self.store.build_context(scope_id="scope_b")
        self.assertIn("ground-state transform", after)

    def test_search_can_intentionally_cross_all_scopes(self):
        self.store.add_episode(
            "Picone attempt failed at the boundary term",
            scope_id="scope_a",
        )
        results = self.store.search_text("Picone")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].scope_id, "scope_a")

    def test_archived_scope_remains_searchable(self):
        self.store.add_episode("Old proof attempt", scope_id="scope_a")
        archived = self.store.archive_scope("scope_a")
        self.assertTrue(archived.archived)
        results = self.store.search_text("Old proof")
        self.assertEqual(len(results), 1)
        activated = self.store.activate_scope("scope_a")
        self.assertFalse(activated.archived)

    def test_7c_database_is_migrated_to_legacy_scope(self):
        legacy_db = self.root / "legacy.db"
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                key TEXT,
                content TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 50,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX idx_memories_working_key
                ON memories(kind, key)
                WHERE kind='working' AND key IS NOT NULL;
            CREATE UNIQUE INDEX idx_memories_artifact_key
                ON memories(kind, key)
                WHERE kind='artifact' AND key IS NOT NULL;
            CREATE TABLE memory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO memories
            (id, kind, key, content, priority, metadata_json, created_at, updated_at)
            VALUES ('old1', 'working', 'proof_strategy', 'old strategy', 70, '{}', 'x', 'x');
            """
        )
        conn.commit()
        conn.close()

        migrated = MemoryStore(legacy_db)
        record = migrated.get("old1")
        self.assertIsNotNone(record)
        self.assertEqual(record.scope_id, "legacy")
        migrated.create_scope("New problem", scope_id="new_scope")
        context = migrated.build_context(scope_id="new_scope")
        self.assertNotIn("old strategy", context)
        self.assertEqual(len(migrated.search_text("old strategy")), 1)

    def test_database_connections_are_released(self):
        self.store.list_scopes()
        self.store.list_memories(scope_ids=["scope_a"])
        self.store.search_text("anything")
        replacement = self.root / "renamed.db"
        self.db_path.replace(replacement)
        self.assertTrue(replacement.exists())


if __name__ == "__main__":
    unittest.main()
