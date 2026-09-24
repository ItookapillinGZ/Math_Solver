import tempfile
import threading
import unittest
from pathlib import Path

from agent_runtime.jobs import JobStatus, JobStore, ThreadJobRunner


class ThreadJobRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "jobs.db"
        self.store = JobStore(self.db_path)
        self.runner = ThreadJobRunner(self.store, runner_prefix="test")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_runner_claims_and_persists_result(self):
        job = self.store.create_job("tool_call", {"value": 4}, source="background")
        self.runner.start(job.id, lambda claimed: {"output": claimed.payload["value"] * 2})
        self.assertTrue(self.runner.wait(job.id, 2))
        saved = self.store.get_job(job.id)
        self.assertEqual(saved.status, JobStatus.SUCCEEDED)
        self.assertEqual(saved.result, {"output": 8})
        self.assertTrue(saved.runner_id.startswith("test-"))

    def test_runner_id_is_internal_not_supplied_by_submitter(self):
        job = self.store.create_job("tool_call", {}, source="background")
        self.assertIsNone(job.runner_id)
        self.runner.start(job.id, lambda claimed: "ok")
        self.assertTrue(self.runner.wait(job.id, 2))
        self.assertIsNotNone(self.store.get_job(job.id).runner_id)

    def test_exception_marks_job_failed(self):
        job = self.store.create_job("tool_call", {}, source="background")
        def boom(_job):
            raise RuntimeError("boom")
        self.runner.start(job.id, boom)
        self.assertTrue(self.runner.wait(job.id, 2))
        saved = self.store.get_job(job.id)
        self.assertEqual(saved.status, JobStatus.FAILED)
        self.assertIn("boom", saved.error)

    def test_resume_queued_starts_matching_jobs(self):
        bg = self.store.create_job("tool_call", {"v": 1}, source="background")
        self.store.create_job("prompt", {"v": 2}, source="cron")
        started = self.runner.resume_queued(
            lambda job: {"output": job.payload["v"]},
            source="background",
            kind="tool_call",
        )
        self.assertEqual(started, [bg.id])
        self.assertTrue(self.runner.wait(bg.id, 2))
        self.assertEqual(self.store.get_job(bg.id).status, JobStatus.SUCCEEDED)

    def test_duplicate_start_in_same_process_executes_once(self):
        gate = threading.Event()
        calls = []
        job = self.store.create_job("tool_call", {}, source="background")
        def execute(_job):
            calls.append(1)
            gate.wait(1)
            return "ok"
        self.assertTrue(self.runner.start(job.id, execute))
        self.assertFalse(self.runner.start(job.id, execute))
        gate.set()
        self.assertTrue(self.runner.wait(job.id, 2))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
