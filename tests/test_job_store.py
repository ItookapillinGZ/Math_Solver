from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_runtime.jobs import JobStateError, JobStatus, JobStore


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "jobs.db"
        self.store = JobStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_and_get_job(self):
        created = self.store.create_job(
            "tool_call",
            {"tool": "bash", "input": {"command": "echo hi"}},
            source="background",
            timeout_seconds=30,
        )
        loaded = self.store.get_job(created.id)
        self.assertEqual(loaded.status, JobStatus.QUEUED)
        self.assertEqual(loaded.kind, "tool_call")
        self.assertEqual(loaded.payload["tool"], "bash")
        self.assertEqual(loaded.source, "background")
        self.assertEqual(loaded.timeout_seconds, 30)

    def test_atomic_claim_allows_exactly_one_runner(self):
        job = self.store.create_job("tool_call", {"tool": "bash"})
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def worker(runner_id: str):
            try:
                local_store = JobStore(self.db_path)
                barrier.wait()
                claimed = local_store.claim_next(runner_id)
                results.append((runner_id, claimed.id if claimed else None))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("runner-a",)),
            threading.Thread(target=worker, args=("runner-b",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        winners = [runner for runner, job_id in results if job_id == job.id]
        self.assertEqual(len(winners), 1)
        loaded = self.store.get_job(job.id)
        self.assertEqual(loaded.status, JobStatus.RUNNING)
        self.assertEqual(loaded.attempt_count, 1)

    def test_successful_lifecycle(self):
        created = self.store.create_job("tool_call", {"tool": "bash"})
        claimed = self.store.claim_next("runner-a")
        self.assertEqual(claimed.id, created.id)
        finished = self.store.mark_succeeded(
            created.id,
            "runner-a",
            {"output": "done"},
        )
        self.assertEqual(finished.status, JobStatus.SUCCEEDED)
        self.assertEqual(finished.result, {"output": "done"})
        self.assertIsNotNone(finished.finished_at)

    def test_wrong_runner_cannot_finish_job(self):
        job = self.store.create_job("tool_call", {})
        self.store.claim_next("runner-a")
        with self.assertRaises(PermissionError):
            self.store.mark_succeeded(job.id, "runner-b", "nope")

    def test_failure_can_requeue_with_retry_budget(self):
        job = self.store.create_job("tool_call", {}, max_attempts=2)
        self.store.claim_next("runner-a")
        retried = self.store.mark_failed(
            job.id,
            "runner-a",
            "temporary failure",
            retry=True,
        )
        self.assertEqual(retried.status, JobStatus.QUEUED)
        self.assertEqual(retried.attempt_count, 1)
        self.assertIsNone(retried.runner_id)

        second = self.store.claim_next("runner-b")
        self.assertEqual(second.id, job.id)
        terminal = self.store.mark_failed(
            job.id,
            "runner-b",
            "failed again",
            retry=True,
        )
        self.assertEqual(terminal.status, JobStatus.FAILED)
        self.assertEqual(terminal.attempt_count, 2)

    def test_future_available_at_is_not_claimed_early(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        delayed = self.store.create_job(
            "scheduled_prompt",
            {"prompt": "later"},
            available_at=future,
        )
        immediate = self.store.create_job(
            "scheduled_prompt",
            {"prompt": "now"},
        )
        claimed = self.store.claim_next("runner-a")
        self.assertEqual(claimed.id, immediate.id)
        self.assertEqual(self.store.get_job(delayed.id).status, JobStatus.QUEUED)

    def test_cancel_queued_job(self):
        job = self.store.create_job("tool_call", {})
        cancelled = self.store.cancel_job(job.id)
        self.assertEqual(cancelled.status, JobStatus.CANCELLED)
        self.assertIsNotNone(cancelled.finished_at)
        self.assertIsNone(self.store.claim_next("runner-a"))

    def test_mark_timed_out(self):
        job = self.store.create_job("tool_call", {}, timeout_seconds=1)
        self.store.claim_next("runner-a")
        timed_out = self.store.mark_timed_out(job.id, "runner-a")
        self.assertEqual(timed_out.status, JobStatus.TIMED_OUT)
        self.assertIn("timed out", timed_out.error)

    def test_recover_interrupted_job_requeues_when_retry_budget_remains(self):
        job = self.store.create_job("tool_call", {}, max_attempts=2)
        self.store.claim_next("runner-a")
        recovered = self.store.recover_interrupted_jobs()
        self.assertEqual([item.id for item in recovered], [job.id])
        current = self.store.get_job(job.id)
        self.assertEqual(current.status, JobStatus.QUEUED)
        self.assertIsNone(current.runner_id)
        self.assertEqual(current.attempt_count, 1)

    def test_recover_interrupted_job_fails_when_retry_budget_exhausted(self):
        job = self.store.create_job("tool_call", {}, max_attempts=1)
        self.store.claim_next("runner-a")
        self.store.recover_interrupted_jobs()
        current = self.store.get_job(job.id)
        self.assertEqual(current.status, JobStatus.FAILED)
        self.assertIn("retry budget exhausted", current.error)

    def test_read_connections_release_database_file(self):
        job = self.store.create_job("tool_call", {})
        self.store.get_job(job.id)
        self.store.list_jobs()
        removable = Path(self.temp_dir.name) / "removable.db"
        other = JobStore(removable)
        created = other.create_job("tool_call", {})
        other.get_job(created.id)
        other.list_jobs()
        os.remove(removable)
        self.assertFalse(removable.exists())

    def test_invalid_transition_is_rejected(self):
        job = self.store.create_job("tool_call", {})
        with self.assertRaises(JobStateError):
            self.store.mark_succeeded(job.id, "runner-a", "bad")

    def test_claim_specific_job(self):
        first = self.store.create_job("tool_call", {"n": 1})
        second = self.store.create_job("tool_call", {"n": 2})
        claimed = self.store.claim_job(second.id, "runner-b")
        self.assertEqual(claimed.id, second.id)
        self.assertEqual(claimed.runner_id, "runner-b")
        self.assertEqual(self.store.get_job(first.id).status, JobStatus.QUEUED)

    def test_list_jobs_filters_source_and_kind(self):
        self.store.create_job("tool_call", {"n": 1}, source="background")
        self.store.create_job("prompt", {"n": 2}, source="cron")
        jobs = self.store.list_jobs(source="background", kind="tool_call")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source, "background")

    def test_terminal_job_can_be_marked_notified_once(self):
        job = self.store.create_job("tool_call", {"n": 1}, source="background")
        claimed = self.store.claim_job(job.id, "runner-a")
        self.store.mark_succeeded(job.id, "runner-a", {"output": "done"})
        pending = self.store.list_unnotified_terminal(source="background")
        self.assertEqual([j.id for j in pending], [job.id])
        updated = self.store.mark_notified(job.id)
        self.assertIsNotNone(updated.notified_at)
        self.assertEqual(self.store.list_unnotified_terminal(source="background"), [])

    def test_non_terminal_job_cannot_be_marked_notified(self):
        job = self.store.create_job("tool_call", {"n": 1})
        with self.assertRaises(JobStateError):
            self.store.mark_notified(job.id)


if __name__ == "__main__":
    unittest.main()
