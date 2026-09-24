from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_runtime.tasks import (
    TaskRuntime,
    TaskRuntimeConfig,
    TaskRuntimeDependencies,
    TaskStore,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class TaskRuntimeTests(unittest.TestCase):
    def make_runtime(self, root: Path, *, clock=None, logs=None) -> TaskRuntime:
        store = TaskStore(
            root / "tasks.db",
            default_lease_seconds=30,
            clock=clock,
        )
        return TaskRuntime(
            TaskRuntimeConfig(legacy_tasks_dir=root / "legacy"),
            TaskRuntimeDependencies(
                store=store,
                terminal_print=(logs if logs is not None else []).append,
                id_factory=lambda: "task_fixed",
            ),
        )

    def test_create_and_list_preserve_affinity_and_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(Path(tmp))
            task = runtime.create_task(
                "prove lemma",
                task_type="math",
                required_roles=["mathematical researcher"],
                tags=["pde"],
            )
            self.assertEqual(task.id, "task_fixed")
            self.assertEqual(task.task_type, "math")
            self.assertEqual(task.required_roles, ["mathematical researcher"])
            self.assertIsNotNone(task.created_at)
            self.assertEqual(runtime.list_tasks()[0].created_at, task.created_at)

    def test_claim_heartbeat_and_complete_keep_existing_text_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = MutableClock()
            logs: list[str] = []
            runtime = self.make_runtime(Path(tmp), clock=clock, logs=logs)
            runtime.create_task("prove lemma")
            self.assertEqual(
                runtime.claim_task("task_fixed", owner="worker"),
                "Claimed task_fixed (prove lemma)",
            )
            heartbeat = runtime.heartbeat_task("task_fixed", owner="worker")
            self.assertIn("Heartbeat renewed for task_fixed until", heartbeat)
            self.assertEqual(
                runtime.complete_task("task_fixed", owner="worker"),
                "Completed task_fixed (prove lemma)",
            )
            self.assertTrue(any("[claim]" in item for item in logs))
            self.assertTrue(any("[complete]" in item for item in logs))

    def test_lead_cannot_claim_or_complete_research_proof_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(Path(tmp))
            runtime.create_task(
                "prove step",
                tags=["research_workflow", "step:S1"],
            )
            self.assertIn("Cannot claim", runtime.claim_task_text("task_fixed"))
            self.assertEqual(runtime.load_task("task_fixed").status, "pending")

            runtime.claim_task("task_fixed", owner="prover")
            self.assertIn("Cannot complete", runtime.complete_task_text("task_fixed"))
            self.assertEqual(runtime.load_task("task_fixed").status, "in_progress")
            with self.assertRaises(PermissionError):
                runtime.complete_task("task_fixed", owner="agent")
            self.assertEqual(
                runtime.complete_task("task_fixed", owner="prover").split()[0],
                "Completed",
            )

    def test_dependency_blocking_contract_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = TaskStore(root / "tasks.db")
            runtime = TaskRuntime(
                TaskRuntimeConfig(legacy_tasks_dir=root / "legacy"),
                TaskRuntimeDependencies(
                    store=store,
                    terminal_print=lambda _text: None,
                    id_factory=iter(["dep", "child"]).__next__,
                ),
            )
            runtime.create_task("dependency")
            runtime.create_task("child", blockedBy=["dep"])
            result = runtime.claim_task("child", owner="worker")
            self.assertIn("Cannot start", result)
            self.assertIn("blocked by", result)

    def test_recovery_returns_expired_lease_to_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = MutableClock()
            runtime = self.make_runtime(Path(tmp), clock=clock)
            runtime.create_task("lease test")
            runtime.claim_task("task_fixed", owner="worker")
            clock.value += timedelta(seconds=31)
            recovered = runtime.recover_expired_task_leases(log=False)
            self.assertEqual([task.id for task in recovered], ["task_fixed"])
            self.assertEqual(runtime.load_task("task_fixed").status, "pending")

    def test_tool_helpers_translate_missing_task_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(Path(tmp))
            self.assertEqual(
                runtime.get_task_text("missing"),
                "Error: task missing not found",
            )
            self.assertEqual(
                runtime.claim_task_text("missing"),
                "Error: task missing not found",
            )

    def test_initialize_migrates_legacy_json_and_recovers_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "task_old.json").write_text(json.dumps({
                "id": "old",
                "subject": "legacy task",
                "description": "",
                "status": "pending",
                "owner": None,
                "blockedBy": [],
            }))
            logs: list[str] = []
            store = TaskStore(root / "tasks.db")
            runtime = TaskRuntime(
                TaskRuntimeConfig(legacy_tasks_dir=legacy),
                TaskRuntimeDependencies(
                    store=store,
                    terminal_print=logs.append,
                ),
            )
            runtime.initialize()
            self.assertEqual(runtime.load_task("old").subject, "legacy task")
            self.assertTrue(any("migrated 1 legacy" in item for item in logs))
            # Import is idempotent: a second initialize does not duplicate it.
            logs.clear()
            runtime.initialize()
            self.assertEqual(len(runtime.list_tasks()), 1)
            self.assertFalse(any("migrated 1 legacy" in item for item in logs))


if __name__ == "__main__":
    unittest.main()
