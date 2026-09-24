from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_runtime.jobs import (
    JobStatus,
    JobStore,
    claim_scheduled_prompts,
    cron_occurrence_job_id,
    enqueue_scheduled_prompt,
    schedule_has_fired,
)


class CronJobIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "jobs.db"
        self.store = JobStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_occurrence_id_is_deterministic(self):
        first = cron_occurrence_job_id("cron_123456", "2026-09-17 20:30")
        second = cron_occurrence_job_id("cron_123456", "2026-09-17 20:30")
        self.assertEqual(first, second)
        self.assertIn("202609172030", first)

    def test_same_cron_occurrence_is_enqueued_once(self):
        first, created_first = enqueue_scheduled_prompt(
            self.store,
            schedule_id="cron_123456",
            cron="*/5 * * * *",
            prompt="check status",
            marker="2026-09-17 20:30",
        )
        second, created_second = enqueue_scheduled_prompt(
            self.store,
            schedule_id="cron_123456",
            cron="*/5 * * * *",
            prompt="check status",
            marker="2026-09-17 20:30",
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.store.list_jobs(source="cron")), 1)

    def test_different_minutes_create_distinct_occurrences(self):
        first, _ = enqueue_scheduled_prompt(
            self.store,
            schedule_id="cron_123456",
            cron="* * * * *",
            prompt="tick",
            marker="2026-09-17 20:30",
        )
        second, _ = enqueue_scheduled_prompt(
            self.store,
            schedule_id="cron_123456",
            cron="* * * * *",
            prompt="tick",
            marker="2026-09-17 20:31",
        )
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(self.store.list_jobs(source="cron")), 2)


    def test_schedule_history_detects_fired_one_shot(self):
        self.assertFalse(schedule_has_fired(self.store, "cron_once"))
        enqueue_scheduled_prompt(
            self.store,
            schedule_id="cron_once",
            cron="30 20 * * *",
            prompt="run once",
            marker="2026-09-17 20:30",
        )
        self.assertTrue(schedule_has_fired(self.store, "cron_once"))

    def test_claim_scheduled_prompts_ignores_background_jobs(self):
        cron_job, _ = enqueue_scheduled_prompt(
            self.store,
            schedule_id="cron_123456",
            cron="* * * * *",
            prompt="tick",
            marker="2026-09-17 20:30",
        )
        background = self.store.create_job(
            "tool_call",
            {"tool_name": "bash"},
            source="background",
        )
        claimed = claim_scheduled_prompts(self.store, "main-agent-cron-test")
        self.assertEqual([job.id for job in claimed], [cron_job.id])
        self.assertEqual(self.store.get_job(cron_job.id).status, JobStatus.RUNNING)
        self.assertEqual(self.store.get_job(background.id).status, JobStatus.QUEUED)

    def test_interrupted_cron_delivery_is_requeued(self):
        cron_job, _ = enqueue_scheduled_prompt(
            self.store,
            schedule_id="cron_123456",
            cron="* * * * *",
            prompt="tick",
            marker="2026-09-17 20:30",
        )
        claimed = claim_scheduled_prompts(self.store, "main-agent-cron-test")
        self.assertEqual(claimed[0].id, cron_job.id)
        self.store.recover_interrupted_jobs(source="cron", kind="scheduled_prompt")
        recovered = self.store.get_job(cron_job.id)
        self.assertEqual(recovered.status, JobStatus.QUEUED)
        self.assertEqual(recovered.attempt_count, 1)

    def test_delivered_cron_job_can_be_marked_succeeded(self):
        cron_job, _ = enqueue_scheduled_prompt(
            self.store,
            schedule_id="cron_123456",
            cron="* * * * *",
            prompt="tick",
            marker="2026-09-17 20:30",
        )
        claimed = claim_scheduled_prompts(self.store, "main-agent-cron-test")
        finished = self.store.mark_succeeded(
            claimed[0].id,
            "main-agent-cron-test",
            {"delivered": True},
        )
        self.assertEqual(finished.status, JobStatus.SUCCEEDED)
        self.assertEqual(finished.result, {"delivered": True})


if __name__ == "__main__":
    unittest.main()
