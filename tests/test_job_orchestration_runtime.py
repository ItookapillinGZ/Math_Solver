from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.jobs import (
    BackgroundJobRuntime,
    BackgroundJobRuntimeDependencies,
    CronSchedulerConfig,
    CronSchedulerDependencies,
    CronSchedulerRuntime,
    JobStatus,
    JobStore,
)


class BackgroundJobRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.tmp.name) / "jobs.db")
        self.logs: list[str] = []
        self.hooks: list[tuple] = []

        def call_handler(handler, args, name):
            if handler is None:
                return f"Unknown: {name}"
            return handler(**args)

        def trigger_hooks(*args):
            self.hooks.append(args)

        self.runtime = BackgroundJobRuntime(
            self.store,
            BackgroundJobRuntimeDependencies(
                call_tool_handler=call_handler,
                trigger_hooks=trigger_hooks,
                terminal_print=self.logs.append,
            ),
            runner_prefix="test-background",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_slow_bash_and_explicit_background_detection(self):
        self.assertTrue(
            self.runtime.should_run("bash", {"command": "python -m pytest"})
        )
        self.assertTrue(
            self.runtime.should_run(
                "bash",
                {"command": "echo hi", "run_in_background": True},
            )
        )
        self.assertFalse(
            self.runtime.should_run("bash", {"command": "echo hi"})
        )
        self.assertFalse(
            self.runtime.should_run("read_file", {"path": "x"})
        )

    def test_start_executes_handler_hook_and_one_time_notification(self):
        block = SimpleNamespace(
            id="tool-1",
            name="bash",
            input={"command": "echo hello", "run_in_background": True},
        )

        def bash(command, run_in_background=False):
            return f"ran:{command}:{run_in_background}"

        job_id = self.runtime.start(block, {"bash": bash})
        self.assertTrue(self.runtime.runner.wait(job_id, timeout=2.0))
        job = self.store.get_job(job_id)
        self.assertEqual(job.status, JobStatus.SUCCEEDED)
        self.assertEqual(job.result["output"], "ran:echo hello:True")
        self.assertEqual(self.hooks[0][0], "PostToolUse")

        notes = self.runtime.collect_notifications()
        self.assertEqual(len(notes), 1)
        self.assertIn(job_id, notes[0])
        self.assertIn("ran:echo hello:True", notes[0])
        self.assertEqual(self.runtime.collect_notifications(), [])

    def test_failed_job_is_reported_as_notification(self):
        job = self.store.create_job(
            "tool_call",
            {"tool_name": "bash", "command": "bad"},
            source="background",
            max_attempts=1,
        )
        claimed = self.store.claim_job(job.id, "runner-a")
        self.assertIsNotNone(claimed)
        self.store.mark_failed(job.id, "runner-a", "boom")
        notes = self.runtime.collect_notifications()
        self.assertEqual(len(notes), 1)
        self.assertIn("failed", notes[0])
        self.assertIn("boom", notes[0])

    def test_list_and_get_background_job_text(self):
        job = self.store.create_job(
            "tool_call",
            {"tool_name": "bash", "command": "echo hi"},
            source="background",
        )
        listed = self.runtime.list_jobs_text()
        self.assertIn(job.id, listed)
        detail = json.loads(self.runtime.get_job_text(job.id))
        self.assertEqual(detail["id"], job.id)
        self.assertEqual(detail["status"], "queued")
        self.assertIn("status must be", self.runtime.list_jobs_text("bogus"))

    def test_build_user_content_and_injection_consume_notifications(self):
        def completed_job(command):
            job = self.store.create_job(
                "tool_call",
                {"tool_name": "bash", "command": command},
                source="background",
            )
            self.store.claim_job(job.id, "runner")
            self.store.mark_succeeded(job.id, "runner", {"output": "done"})
            return job

        first = completed_job("one")
        content = self.runtime.build_user_content(
            [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]
        )
        self.assertEqual(content[0]["type"], "tool_result")
        self.assertIn(first.id, content[1]["text"])

        second = completed_job("two")
        messages: list[dict] = []
        self.runtime.inject_notifications(messages)
        self.assertEqual(len(messages), 1)
        self.assertIn(second.id, messages[0]["content"][0]["text"])


class CronSchedulerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = JobStore(self.root / "jobs.db")
        self.logs: list[str] = []
        self.runtime = CronSchedulerRuntime(
            self.store,
            CronSchedulerConfig(
                legacy_path=self.root / ".scheduled_tasks.json",
                runner_id="cron-runner-test",
                session_id="session-a",
                poll_interval_seconds=0.01,
            ),
            CronSchedulerDependencies(
                terminal_print=self.logs.append,
                now=lambda: datetime(2026, 9, 20, 10, 15),
                sleep=lambda _seconds: None,
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_cron_validation_and_matching(self):
        self.assertIsNone(self.runtime.validate("*/5 * * * *"))
        self.assertIn("minute", self.runtime.validate("99 * * * *"))
        dt = datetime(2026, 9, 20, 10, 15)
        self.assertTrue(self.runtime.matches("*/5 10 * * *", dt))
        self.assertFalse(self.runtime.matches("*/10 10 * * *", dt))

    def test_durable_and_session_schedule_visibility(self):
        durable = self.runtime.schedule("* * * * *", "durable", durable=True)
        session = self.runtime.schedule("* * * * *", "session", durable=False)
        ids = {item.id for item in self.runtime.visible_schedules()}
        self.assertIn(durable.id, ids)
        self.assertIn(session.id, ids)

        other = CronSchedulerRuntime(
            self.store,
            CronSchedulerConfig(
                legacy_path=self.root / "unused.json",
                runner_id="other-runner",
                session_id="session-b",
            ),
            CronSchedulerDependencies(terminal_print=lambda _text: None),
        )
        other_ids = {item.id for item in other.visible_schedules()}
        self.assertIn(durable.id, other_ids)
        self.assertNotIn(session.id, other_ids)
        self.assertEqual(other.cancel(session.id), f"Job {session.id} not found")

    def test_tick_is_idempotent_and_one_shot_deactivates(self):
        schedule = self.runtime.schedule(
            "* * * * *",
            "once",
            recurring=False,
            durable=True,
        )
        now = datetime(2026, 9, 20, 10, 15)
        created = self.runtime.tick(now)
        self.assertEqual(len(created), 1)
        self.assertEqual(self.runtime.tick(now), [])
        stored = self.store.get_schedule(schedule.id)
        self.assertFalse(stored.active)
        jobs = self.store.list_jobs(source="cron", kind="scheduled_prompt")
        self.assertEqual(len(jobs), 1)

    def test_legacy_migration_imports_once_and_removes_file(self):
        path = self.runtime.config.legacy_path
        path.write_text(
            json.dumps(
                [
                    {
                        "id": "legacy-one",
                        "cron": "*/5 * * * *",
                        "prompt": "legacy prompt",
                        "recurring": True,
                        "durable": True,
                    }
                ]
            ),
            encoding="utf-8",
        )
        imported, skipped = self.runtime.migrate_legacy_file()
        self.assertEqual((imported, skipped), (1, 0))
        self.assertFalse(path.exists())
        self.assertEqual(self.store.get_schedule("legacy-one").prompt, "legacy prompt")
        self.assertEqual(self.runtime.migrate_legacy_file(), (0, 0))

    def test_recover_interrupted_cron_occurrence_requeues_it(self):
        self.runtime.schedule("* * * * *", "recover me")
        created = self.runtime.tick(datetime(2026, 9, 20, 10, 15))
        job_id = created[0]
        claimed = self.store.claim_job(job_id, "crashed-runner")
        self.assertIsNotNone(claimed)
        self.assertEqual(self.store.get_job(job_id).status, JobStatus.RUNNING)
        self.runtime.recover_interrupted()
        self.assertEqual(self.store.get_job(job_id).status, JobStatus.QUEUED)

    def test_deliver_claimed_once_updates_history_context_and_job(self):
        self.runtime.schedule("* * * * *", "scheduled theorem check")
        [job_id] = self.runtime.tick(datetime(2026, 9, 20, 10, 15))
        history: list[dict] = []
        context = {"before": True}
        calls: list[str] = []

        def agent_loop(messages, ctx):
            calls.append("agent")
            self.assertIn("[Scheduled] scheduled theorem check", messages[-1]["content"])
            self.assertTrue(ctx["before"])

        def update_context(ctx, messages):
            return {"after": len(messages)}

        def print_assistants(messages, turn_start):
            calls.append(f"print:{turn_start}")

        delivered = self.runtime.deliver_claimed_once(
            history,
            context,
            agent_lock=threading.Lock(),
            agent_loop=agent_loop,
            update_context=update_context,
            print_turn_assistants=print_assistants,
        )
        self.assertEqual(delivered, 1)
        self.assertEqual(calls, ["agent", "print:0"])
        self.assertEqual(context["after"], 1)
        self.assertEqual(self.store.get_job(job_id).status, JobStatus.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
