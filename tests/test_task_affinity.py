import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_runtime.tasks import (
    TaskRecord,
    TaskStore,
    affinity_score,
    infer_worker_categories,
    select_tasks_for_worker,
    task_matches_worker,
)


class TaskAffinityTests(unittest.TestCase):
    def record(self, *, task_type="general", required_roles=(), created_at="2026-01-01T00:00:00+00:00"):
        return TaskRecord(
            id=f"task_{task_type}_{len(required_roles)}",
            subject="test",
            description="",
            status="pending",
            owner=None,
            blocked_by=(),
            task_type=task_type,
            required_roles=tuple(required_roles),
            created_at=created_at,
        )

    def test_math_worker_matches_math_task(self):
        task = self.record(task_type="math")
        self.assertTrue(task_matches_worker(task, "mathematical researcher"))

    def test_math_worker_does_not_match_engineering_task(self):
        task = self.record(task_type="engineering")
        self.assertFalse(task_matches_worker(task, "mathematical researcher"))

    def test_specialized_worker_does_not_auto_claim_untyped_general_task(self):
        task = self.record(task_type="general")
        self.assertIsNone(affinity_score(task, "mathematical researcher"))

    def test_explicit_required_role_can_make_general_task_eligible(self):
        task = self.record(
            task_type="general",
            required_roles=("mathematical researcher",),
        )
        self.assertTrue(task_matches_worker(task, "Senior Mathematical Researcher"))

    def test_reviewer_role_is_a_math_worker(self):
        categories = infer_worker_categories(
            "Senior Mathematical Reviewer & Journal Referee"
        )
        self.assertIn("math", categories)

    def test_selection_prefers_explicit_role_match(self):
        typed = self.record(task_type="math", created_at="2026-01-01T00:00:00+00:00")
        explicit = self.record(
            task_type="math",
            required_roles=("mathematical researcher",),
            created_at="2026-01-02T00:00:00+00:00",
        )
        selected = select_tasks_for_worker(
            [typed, explicit], "mathematical researcher"
        )
        self.assertIs(selected[0], explicit)


class TaskAffinityStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tasks.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_affinity_metadata_round_trips(self):
        store = TaskStore(self.db_path)
        created = store.create_task(
            task_id="task_math",
            subject="prove lemma",
            task_type="math",
            required_roles=["mathematical researcher"],
            tags=["pde", "bifurcation"],
        )
        loaded = store.get_task(created.id)
        self.assertEqual(loaded.task_type, "math")
        self.assertEqual(loaded.required_roles, ("mathematical researcher",))
        self.assertEqual(loaded.tags, ("pde", "bifurcation"))

    def test_old_database_is_migrated_in_place(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                owner TEXT,
                worktree TEXT,
                lease_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE task_dependencies (
                task_id TEXT NOT NULL,
                depends_on_task_id TEXT NOT NULL,
                PRIMARY KEY (task_id, depends_on_task_id)
            );
            INSERT INTO tasks (
                id, subject, description, status, owner, worktree,
                lease_until, created_at, updated_at
            ) VALUES (
                'legacy', 'old task', '', 'pending', NULL, NULL,
                NULL, '2026-01-01', '2026-01-01'
            );
            """
        )
        conn.commit()
        conn.close()

        store = TaskStore(self.db_path)
        task = store.get_task("legacy")
        self.assertEqual(task.task_type, "general")
        self.assertEqual(task.required_roles, ())
        self.assertEqual(task.tags, ())

    def test_invalid_task_type_is_rejected(self):
        store = TaskStore(self.db_path)
        with self.assertRaises(ValueError):
            store.create_task(
                task_id="bad",
                subject="bad",
                task_type="unknown",
            )


if __name__ == "__main__":
    unittest.main()
