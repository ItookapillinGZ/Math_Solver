from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_runtime.tasks import TaskStore


class FakeClock:
    def __init__(self):
        self.current = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.current

    def advance(self, seconds: int):
        self.current += timedelta(seconds=seconds)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tasks.db"
        self.store = TaskStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_and_get_task(self):
        created = self.store.create_task(
            task_id="task_1",
            subject="First task",
            description="demo",
        )

        loaded = self.store.get_task("task_1")

        self.assertIsNotNone(loaded)
        self.assertEqual(created.id, loaded.id)
        self.assertEqual("pending", loaded.status)
        self.assertEqual((), loaded.blocked_by)

    def test_missing_dependency_prevents_claim(self):
        self.store.create_task(
            task_id="task_child",
            subject="Child",
            blocked_by=["task_missing"],
        )

        result = self.store.claim_task("task_child", owner="worker-a")

        self.assertFalse(result.claimed)
        self.assertEqual(("task_missing",), result.missing_dependencies)

    def test_incomplete_dependency_prevents_claim(self):
        self.store.create_task(task_id="task_parent", subject="Parent")
        self.store.create_task(
            task_id="task_child",
            subject="Child",
            blocked_by=["task_parent"],
        )

        result = self.store.claim_task("task_child", owner="worker-a")

        self.assertFalse(result.claimed)
        self.assertEqual(("task_parent",), result.blocked_by)

    def test_completed_dependency_allows_claim(self):
        self.store.create_task(task_id="task_parent", subject="Parent")
        parent_claim = self.store.claim_task("task_parent", owner="worker-parent")
        self.assertTrue(parent_claim.claimed)
        self.store.complete_task("task_parent", owner="worker-parent")

        self.store.create_task(
            task_id="task_child",
            subject="Child",
            blocked_by=["task_parent"],
        )
        child_claim = self.store.claim_task("task_child", owner="worker-child")

        self.assertTrue(child_claim.claimed)
        self.assertEqual("in_progress", child_claim.task.status)
        self.assertEqual("worker-child", child_claim.task.owner)

    def test_complete_requires_in_progress(self):
        self.store.create_task(task_id="task_1", subject="First")

        with self.assertRaises(ValueError):
            self.store.complete_task("task_1", owner="worker-a")

    def test_complete_requires_claim_owner(self):
        self.store.create_task(task_id="task_1", subject="First")
        self.store.claim_task("task_1", owner="prover")

        with self.assertRaisesRegex(PermissionError, "owned by prover"):
            self.store.complete_task("task_1", owner="agent")

        self.assertEqual(self.store.get_task("task_1").status, "in_progress")
        self.assertEqual(
            self.store.complete_task("task_1", owner="prover").status,
            "completed",
        )

    def test_complete_rejects_expired_lease(self):
        clock = FakeClock()
        store = TaskStore(self.db_path, clock=clock, default_lease_seconds=30)
        store.create_task(task_id="task_expired", subject="Expired")
        store.claim_task("task_expired", owner="prover")
        clock.advance(31)

        with self.assertRaisesRegex(RuntimeError, "lease has expired"):
            store.complete_task("task_expired", owner="prover")

        self.assertEqual(store.get_task("task_expired").status, "in_progress")

    def test_worktree_update_is_persisted(self):
        self.store.create_task(task_id="task_1", subject="First")

        updated = self.store.update_worktree("task_1", "worker-tree")
        loaded = self.store.get_task("task_1")

        self.assertEqual("worker-tree", updated.worktree)
        self.assertEqual("worker-tree", loaded.worktree)

    def test_atomic_claim_allows_exactly_one_winner(self):
        self.store.create_task(task_id="task_race", subject="Race")
        barrier = threading.Barrier(2)
        results = []
        errors = []
        lock = threading.Lock()

        def worker(owner: str):
            try:
                local_store = TaskStore(self.db_path)
                barrier.wait(timeout=5)
                result = local_store.claim_task("task_race", owner=owner)
                with lock:
                    results.append(result)
            except Exception as exc:  # pragma: no cover - diagnostic path
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("worker-a",)),
            threading.Thread(target=worker, args=("worker-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        winners = [result for result in results if result.claimed]
        losers = [result for result in results if not result.claimed]
        self.assertEqual(1, len(winners))
        self.assertEqual(1, len(losers))

        final_task = self.store.get_task("task_race")
        self.assertEqual("in_progress", final_task.status)
        self.assertIn(final_task.owner, {"worker-a", "worker-b"})

    def test_claim_sets_default_lease(self):
        clock = FakeClock()
        store = TaskStore(
            Path(self.temp_dir.name) / "lease-default.db",
            default_lease_seconds=60,
            clock=clock,
        )
        store.create_task(task_id="task_lease", subject="Lease")

        result = store.claim_task("task_lease", owner="worker-a")

        self.assertTrue(result.claimed)
        self.assertEqual(
            (clock.current + timedelta(seconds=60)).isoformat(),
            result.task.lease_until,
        )

    def test_heartbeat_extends_lease(self):
        clock = FakeClock()
        store = TaskStore(
            Path(self.temp_dir.name) / "heartbeat.db",
            default_lease_seconds=60,
            clock=clock,
        )
        store.create_task(task_id="task_hb", subject="Heartbeat")
        first = store.claim_task("task_hb", owner="worker-a")
        old_deadline = first.task.lease_until

        clock.advance(20)
        renewed = store.heartbeat_task("task_hb", owner="worker-a")

        self.assertGreater(renewed.lease_until, old_deadline)
        self.assertEqual(
            (clock.current + timedelta(seconds=60)).isoformat(),
            renewed.lease_until,
        )

    def test_heartbeat_rejects_wrong_owner(self):
        clock = FakeClock()
        store = TaskStore(
            Path(self.temp_dir.name) / "wrong-owner.db",
            default_lease_seconds=60,
            clock=clock,
        )
        store.create_task(task_id="task_owner", subject="Owner")
        store.claim_task("task_owner", owner="worker-a")

        with self.assertRaises(PermissionError):
            store.heartbeat_task("task_owner", owner="worker-b")

    def test_heartbeat_rejects_expired_lease(self):
        clock = FakeClock()
        store = TaskStore(
            Path(self.temp_dir.name) / "expired-heartbeat.db",
            default_lease_seconds=30,
            clock=clock,
        )
        store.create_task(task_id="task_expired", subject="Expired")
        store.claim_task("task_expired", owner="worker-a")
        clock.advance(31)

        with self.assertRaises(RuntimeError):
            store.heartbeat_task("task_expired", owner="worker-a")

    def test_recover_expired_task_returns_it_to_pending(self):
        clock = FakeClock()
        store = TaskStore(
            Path(self.temp_dir.name) / "recover.db",
            default_lease_seconds=30,
            clock=clock,
        )
        store.create_task(task_id="task_recover", subject="Recover")
        store.claim_task("task_recover", owner="dead-worker")
        clock.advance(31)

        recovered = store.recover_expired_tasks()
        task = store.get_task("task_recover")

        self.assertEqual(["task_recover"], [item.id for item in recovered])
        self.assertEqual("pending", task.status)
        self.assertIsNone(task.owner)
        self.assertIsNone(task.lease_until)

    def test_claim_can_recover_and_reassign_expired_task_atomically(self):
        clock = FakeClock()
        store = TaskStore(
            Path(self.temp_dir.name) / "reclaim.db",
            default_lease_seconds=30,
            clock=clock,
        )
        store.create_task(task_id="task_reclaim", subject="Reclaim")
        store.claim_task("task_reclaim", owner="worker-a")
        clock.advance(31)

        result = store.claim_task("task_reclaim", owner="worker-b")

        self.assertTrue(result.claimed)
        self.assertEqual("worker-b", result.task.owner)
        self.assertEqual("in_progress", result.task.status)
        self.assertIsNotNone(result.task.lease_until)

    def test_unleased_legacy_in_progress_task_is_not_recovered(self):
        clock = FakeClock()
        store = TaskStore(
            Path(self.temp_dir.name) / "legacy-unleased.db",
            default_lease_seconds=30,
            clock=clock,
        )
        from agent_runtime.tasks import TaskRecord
        store.import_task(TaskRecord(
            id="task_legacy",
            subject="Legacy",
            description="",
            status="in_progress",
            owner="legacy-worker",
            blocked_by=(),
            lease_until=None,
        ))
        clock.advance(3600)

        recovered = store.recover_expired_tasks()
        task = store.get_task("task_legacy")

        self.assertEqual([], recovered)
        self.assertEqual("in_progress", task.status)
        self.assertEqual("legacy-worker", task.owner)
        self.assertIsNone(task.lease_until)

    def test_read_connections_release_database_file(self):
        """Regression test for Windows WinError 32 from unclosed read handles."""
        self.store.create_task(task_id="task_release", subject="Release")
        self.store.get_task("task_release")
        self.store.list_tasks()
        self.store.can_start("task_release")

        # On Windows this fails with PermissionError if a read connection is
        # still holding the SQLite database open.
        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())

    def test_legacy_json_migration_preserves_state_and_dependencies(self):
        legacy_dir = Path(self.temp_dir.name) / ".tasks"
        legacy_dir.mkdir()
        (legacy_dir / "task_parent.json").write_text(
            json.dumps({
                "id": "task_parent",
                "subject": "Parent",
                "description": "done",
                "status": "completed",
                "owner": "worker-a",
                "blockedBy": [],
                "worktree": "wt-parent",
            }),
            encoding="utf-8",
        )
        (legacy_dir / "task_child.json").write_text(
            json.dumps({
                "id": "task_child",
                "subject": "Child",
                "description": "waiting",
                "status": "pending",
                "owner": None,
                "blockedBy": ["task_parent"],
                "worktree": None,
            }),
            encoding="utf-8",
        )

        imported, errors = self.store.import_legacy_json_dir(legacy_dir)

        self.assertEqual(2, imported)
        self.assertEqual([], errors)
        parent = self.store.get_task("task_parent")
        child = self.store.get_task("task_child")
        self.assertEqual("completed", parent.status)
        self.assertEqual("worker-a", parent.owner)
        self.assertEqual("wt-parent", parent.worktree)
        self.assertEqual(("task_parent",), child.blocked_by)
        self.assertTrue(self.store.can_start("task_child"))

        imported_again, errors_again = self.store.import_legacy_json_dir(legacy_dir)
        self.assertEqual(0, imported_again)
        self.assertEqual([], errors_again)

    def test_malformed_legacy_json_is_reported_not_fatal(self):
        legacy_dir = Path(self.temp_dir.name) / ".tasks"
        legacy_dir.mkdir()
        (legacy_dir / "task_bad.json").write_text("{not valid json", encoding="utf-8")

        imported, errors = self.store.import_legacy_json_dir(legacy_dir)

        self.assertEqual(0, imported)
        self.assertEqual(1, len(errors))
        self.assertIn("task_bad.json", errors[0])


if __name__ == "__main__":
    unittest.main()
