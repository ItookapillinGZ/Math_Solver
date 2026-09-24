import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.observability import TraceStore
from agent_runtime.research import (
    ResearchWorkflowCoordinator,
    ResearchWorkflowOrchestrator,
    ResearchWorkflowOrchestratorDependencies,
)


class FakeRuntime:
    def __init__(self):
        self.tasks = {}
        self.counter = 0
        self.spawned = []

    def create_task(self, **kwargs):
        self.counter += 1
        task = SimpleNamespace(
            id=f"task_{self.counter}",
            status="pending",
            worktree=None,
            **kwargs,
        )
        self.tasks[task.id] = task
        return task

    def load_task(self, task_id):
        return self.tasks[task_id]

    def spawn(self, name, role, prompt):
        self.spawned.append((name, role, prompt))
        return f"spawned {name}"

    def send(self, to, content):
        return "sent"

    def create_worktree(self, name, task_id):
        for task in self.tasks.values():
            if task.id == task_id:
                task.worktree = name
        return f"created {name}"

    def bind_worktree(self, task_id, name):
        self.tasks[task_id].worktree = name


class ResearchWorkflowPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TraceStore(Path(self.tmp.name) / "traces.db")
        self.fake = FakeRuntime()

    def tearDown(self):
        self.tmp.cleanup()

    def make_orchestrator(self):
        return ResearchWorkflowOrchestrator(
            ResearchWorkflowCoordinator(),
            ResearchWorkflowOrchestratorDependencies(
                create_task=self.fake.create_task,
                load_task=self.fake.load_task,
                spawn_teammate=self.fake.spawn,
                send_message=self.fake.send,
                create_worktree=self.fake.create_worktree,
                bind_task_to_worktree=self.fake.bind_worktree,
                terminal_print=lambda _text: None,
                record_event=lambda event, run_id, payload: self.store.record_workflow_event(event, run_id, payload),
                save_snapshot=self.store.save_workflow_snapshot,
                load_snapshots=self.store.load_workflow_snapshots,
            ),
        )

    def test_dispatch_persists_and_restores_run_and_binding(self):
        o1 = self.make_orchestrator()
        started = json.loads(o1.dispatch("start", {"problem_ref": "problem.tex"}))
        run_id = started["result"]["id"]
        o1.record_literature_tool_result(
            run_id, "mcp__docs__search", {"query": "energy"},
            "Arxiv_ID: 2203.12345\nTitle: Energy methods\nSummary: fixture",
        )
        o1.dispatch(
            "literature",
            {
                "run_id": run_id,
                "difficulty": "hard",
                "literature_notes": "Energy methods found",
                "selected_sources": [{"source_id": "2203.12345", "relevance_note": "Energy estimate"}],
                "auto_decompose": False,
                "methods": [{"name": "energy", "rationale": "candidate"}],
            },
        )
        method_id = o1.runs[run_id].methods[0].id
        activated = json.loads(
            o1.dispatch(
                "activate_method",
                {
                    "run_id": run_id,
                    "method_id": method_id,
                    "steps": [
                        {"id": "S1", "goal": "first", "depends_on": []},
                        {"id": "S2", "goal": "second", "depends_on": ["S1"]},
                    ],
                },
            )
        )
        self.assertTrue(activated["ok"])
        snapshot = self.store.research_snapshot(run_id)
        self.assertEqual(snapshot["run"]["stage"], "methods_active")
        self.assertEqual(len(snapshot["bindings"]), 1)

        o2 = self.make_orchestrator()
        self.assertIn(run_id, o2.runs)
        restored = o2.bindings[(run_id, method_id)]
        self.assertEqual(restored.step_order, ("S1", "S2"))
        self.assertEqual(len(restored.step_task_ids), 2)

    def test_dispatch_records_workflow_events(self):
        o = self.make_orchestrator()
        result = json.loads(o.dispatch("start", {"problem_ref": "problem.tex"}))
        run_id = result["result"]["id"]
        events = self.store.list_workflow_events(run_id)
        self.assertEqual(events[-1]["event_type"], "start")
        self.assertEqual(events[-1]["payload"]["research_stage"], "literature_survey")


if __name__ == "__main__":
    unittest.main()
