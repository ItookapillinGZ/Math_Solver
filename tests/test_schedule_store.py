from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_runtime.jobs import (
    JobStateError,
    JobStore,
    enqueue_scheduled_prompt,
    migrate_legacy_schedule_items,
    reconcile_one_shot_schedules,
)


def validate_cron_for_test(expr: str) -> str | None:
    return None if len(expr.split()) == 5 else "invalid"


class ScheduleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "jobs.db"
        self.store = JobStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_durable_schedule_persists_across_store_instances(self):
        created = self.store.create_schedule(
            "*/5 * * * *", "check status", recurring=True, durable=True
        )
        reopened = JobStore(self.db_path)
        loaded = reopened.get_schedule(created.id)
        self.assertEqual(loaded.cron, "*/5 * * * *")
        self.assertTrue(loaded.active)
        self.assertTrue(loaded.durable)

    def test_session_schedule_requires_session_id(self):
        with self.assertRaises(ValueError):
            self.store.create_schedule(
                "* * * * *", "session only", durable=False
            )

    def test_session_visibility_filters_other_sessions(self):
        durable = self.store.create_schedule(
            "* * * * *", "durable", durable=True
        )
        mine = self.store.create_schedule(
            "* * * * *", "mine", durable=False, session_id="session-a"
        )
        self.store.create_schedule(
            "* * * * *", "other", durable=False, session_id="session-b"
        )
        visible = self.store.list_schedules(active_only=True, session_id="session-a")
        self.assertEqual({row.id for row in visible}, {durable.id, mine.id})

    def test_mark_recurring_schedule_fired_keeps_it_active(self):
        schedule = self.store.create_schedule(
            "* * * * *", "tick", recurring=True
        )
        updated = self.store.mark_schedule_fired(schedule.id, "2026-09-17 20:30")
        self.assertTrue(updated.active)
        self.assertEqual(updated.last_fired_marker, "2026-09-17 20:30")

    def test_mark_one_shot_fired_deactivates_it(self):
        schedule = self.store.create_schedule(
            "30 20 * * *", "once", recurring=False
        )
        updated = self.store.mark_schedule_fired(schedule.id, "2026-09-17 20:30")
        self.assertFalse(updated.active)
        self.assertEqual(updated.last_fired_marker, "2026-09-17 20:30")

    def test_cancel_preserves_schedule_history(self):
        schedule = self.store.create_schedule(
            "* * * * *", "cancel me"
        )
        cancelled = self.store.cancel_schedule(schedule.id)
        self.assertFalse(cancelled.active)
        self.assertIsNotNone(cancelled.cancelled_at)
        self.assertEqual(self.store.get_schedule(schedule.id).prompt, "cancel me")

    def test_create_schedule_once_is_idempotent(self):
        first, created_first = self.store.create_schedule_once(
            "* * * * *", "tick", schedule_id="cron_fixed"
        )
        second, created_second = self.store.create_schedule_once(
            "* * * * *", "tick", schedule_id="cron_fixed"
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.id, second.id)

    def test_create_schedule_once_rejects_collision(self):
        self.store.create_schedule_once(
            "* * * * *", "first", schedule_id="cron_fixed"
        )
        with self.assertRaises(JobStateError):
            self.store.create_schedule_once(
                "* * * * *", "different", schedule_id="cron_fixed"
            )

    def test_legacy_items_migrate_idempotently(self):
        items = [{
            "id": "cron_legacy",
            "cron": "*/5 * * * *",
            "prompt": "legacy prompt",
            "recurring": True,
            "durable": True,
        }]
        imported, skipped = migrate_legacy_schedule_items(
            self.store, items, validate_cron=validate_cron_for_test
        )
        imported_again, skipped_again = migrate_legacy_schedule_items(
            self.store, items, validate_cron=validate_cron_for_test
        )
        self.assertEqual((imported, skipped), (1, 0))
        self.assertEqual((imported_again, skipped_again), (0, 0))
        self.assertEqual(self.store.get_schedule("cron_legacy").prompt, "legacy prompt")

    def test_migration_skips_fired_one_shot(self):
        enqueue_scheduled_prompt(
            self.store,
            schedule_id="cron_once",
            cron="30 20 * * *",
            prompt="once",
            marker="2026-09-17 20:30",
        )
        imported, skipped = migrate_legacy_schedule_items(
            self.store,
            [{
                "id": "cron_once",
                "cron": "30 20 * * *",
                "prompt": "once",
                "recurring": False,
                "durable": True,
            }],
            validate_cron=validate_cron_for_test,
        )
        self.assertEqual((imported, skipped), (0, 1))
        with self.assertRaises(KeyError):
            self.store.get_schedule("cron_once")

    def test_reconcile_deactivates_crash_window_one_shot(self):
        schedule = self.store.create_schedule(
            "30 20 * * *", "once", recurring=False, durable=True,
            schedule_id="cron_once",
        )
        # Simulate the crash window: the occurrence was durably enqueued, but
        # the runtime died before mark_schedule_fired() deactivated the schedule.
        enqueue_scheduled_prompt(
            self.store,
            schedule_id=schedule.id,
            cron=schedule.cron,
            prompt=schedule.prompt,
            marker="2026-09-17 20:30",
        )
        self.assertTrue(self.store.get_schedule(schedule.id).active)
        count = reconcile_one_shot_schedules(
            self.store, [self.store.get_schedule(schedule.id)]
        )
        self.assertEqual(count, 1)
        self.assertFalse(self.store.get_schedule(schedule.id).active)

    def test_schedule_reads_release_database_file(self):
        schedule = self.store.create_schedule("* * * * *", "tick")
        self.store.get_schedule(schedule.id)
        self.store.list_schedules(active_only=True)
        # On Windows this unlink fails with WinError 32 if a read connection
        # remains open after the public method returns.
        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
