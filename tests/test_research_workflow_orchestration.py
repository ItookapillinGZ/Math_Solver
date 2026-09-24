from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field

from agent_runtime.research.orchestration import (
    ResearchWorkflowOrchestrator,
    ResearchWorkflowOrchestratorDependencies,
)
from agent_runtime.research.workflow import ResearchWorkflowCoordinator
from agent_runtime.research.workflow_models import MethodStage, ResearchStage


@dataclass
class FakeTask:
    id: str
    subject: str
    description: str
    status: str = "pending"
    owner: str | None = None
    blockedBy: list[str] = field(default_factory=list)
    task_type: str = "general"
    required_roles: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


class FakeTaskRuntime:
    def __init__(self):
        self.tasks: dict[str, FakeTask] = {}
        self.created: list[FakeTask] = []

    def create_task(
        self,
        subject: str,
        description: str = "",
        blockedBy=None,
        task_type="general",
        required_roles=None,
        tags=None,
    ):
        task_id = f"task_{len(self.tasks) + 1:03d}"
        task = FakeTask(
            id=task_id,
            subject=subject,
            description=description,
            blockedBy=list(blockedBy or []),
            task_type=task_type,
            required_roles=list(required_roles or []),
            tags=list(tags or []),
        )
        self.tasks[task_id] = task
        self.created.append(task)
        return task

    def load_task(self, task_id: str):
        if task_id not in self.tasks:
            raise FileNotFoundError(task_id)
        return self.tasks[task_id]


class FakeTeam:
    def __init__(self):
        self.spawned: list[tuple[str, str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.active: set[str] = set()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        if name in self.active:
            return f"Teammate '{name}' already exists"
        self.active.add(name)
        self.spawned.append((name, role, prompt))
        return f"Teammate '{name}' spawned as {role}"

    def send(self, to: str, content: str) -> str:
        self.sent.append((to, content))
        return f"Sent to {to}"


class FakeWorktrees:
    def __init__(self, tasks: FakeTaskRuntime):
        self.tasks = tasks
        self.created: list[tuple[str, str]] = []
        self.bound: list[tuple[str, str]] = []

    def create(self, name: str, task_id: str) -> str:
        self.created.append((name, task_id))
        setattr(self.tasks.tasks[task_id], "worktree", name)
        return f"Worktree '{name}' created"

    def bind(self, task_id: str, name: str):
        self.bound.append((task_id, name))
        setattr(self.tasks.tasks[task_id], "worktree", name)


class ResearchWorkflowOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tasks = FakeTaskRuntime()
        self.team = FakeTeam()
        self.worktrees = FakeWorktrees(self.tasks)
        self.logs = []
        workflow = ResearchWorkflowCoordinator(run_id_factory=lambda: "research_demo")
        self.orch = ResearchWorkflowOrchestrator(
            workflow,
            ResearchWorkflowOrchestratorDependencies(
                create_task=self.tasks.create_task,
                load_task=self.tasks.load_task,
                spawn_teammate=self.team.spawn,
                send_message=self.team.send,
                create_worktree=self.worktrees.create,
                bind_task_to_worktree=self.worktrees.bind,
                terminal_print=self.logs.append,
            ),
        )
        self.run = self.orch.start_run("problem.tex")

    def literature(self, methods=None):
        self.orch.record_literature_tool_result(
            self.run.id, "mcp__docs__search", {"query": "bifurcation"},
            "Arxiv_ID: 2203.12345\nTitle: Simple eigenvalue bifurcation\nSummary: fixture",
        )
        return self.orch.complete_literature_survey(
            self.run.id,
            difficulty="hard",
            literature_notes="survey",
            selected_sources=[{"source_id": "2203.12345", "relevance_note": "Relevant to the problem"}],
            methods=methods
            or [
                {"name": "bifurcation", "rationale": "spectral crossing"},
                {"name": "energy", "rationale": "a priori estimates"},
            ],
        )

    def activate_first(self):
        self.literature(methods=[{"name": "bifurcation", "rationale": "spectral"}])
        return self.orch.activate_method(
            self.run.id,
            "method_01",
            steps=[
                {"id": "A3", "goal": "transversality", "depends_on": ["A2"]},
                {"id": "A1", "goal": "Fredholm property", "depends_on": []},
                {"id": "A2", "goal": "kernel", "depends_on": ["A1"]},
            ],
        )

    def complete_bound_tasks(self, binding):
        for step_id in binding.step_order:
            self.tasks.tasks[binding.step_task_ids[step_id]].status = "completed"

    def reach_structural(self):
        binding = self.activate_first()
        self.complete_bound_tasks(binding)
        method = self.orch.sync_method(self.run.id, "method_01")
        self.assertEqual(method.stage, MethodStage.STRUCTURAL_VERIFY)
        return binding, method

    def test_literature_spawns_one_decomposer_per_method(self):
        self.literature()
        self.assertEqual(self.run.stage, ResearchStage.METHODS_ACTIVE)
        names = [item[0] for item in self.team.spawned]
        self.assertEqual(len(names), 2)
        self.assertTrue(all(name.startswith("decomposer_") for name in names))
        self.assertIn("Return ONE JSON object", self.team.spawned[0][2])

    def test_activate_method_materializes_topological_durable_tasks(self):
        binding = self.activate_first()
        self.assertEqual(binding.step_order, ("A1", "A2", "A3"))
        self.assertEqual(len(self.tasks.created), 3)
        a1 = self.tasks.tasks[binding.step_task_ids["A1"]]
        a2 = self.tasks.tasks[binding.step_task_ids["A2"]]
        a3 = self.tasks.tasks[binding.step_task_ids["A3"]]
        self.assertEqual(a1.blockedBy, [])
        self.assertEqual(a2.blockedBy, [a1.id])
        self.assertEqual(a3.blockedBy, [a2.id])
        self.assertEqual(a1.task_type, "math")
        self.assertEqual(a1.required_roles, [binding.prover_role])
        self.assertIn("research_run:research_demo", a1.tags)
        self.assertIn("plan_generation:1", a1.tags)
        self.assertEqual(self.run.get_method("method_01").teammate, binding.prover_name)
        self.assertIsNotNone(binding.worktree_name)
        self.assertEqual(getattr(a1, "worktree", None), binding.worktree_name)
        self.assertEqual(getattr(a2, "worktree", None), binding.worktree_name)
        self.assertIn(f".worktrees/{binding.worktree_name}/", binding.review_path())

    def test_prover_prompt_uses_taskstore_as_authoritative_plan(self):
        binding = self.activate_first()
        spawn = next(item for item in self.team.spawned if item[0] == binding.prover_name)
        self.assertIn("durable TaskStore is authoritative", spawn[2])
        self.assertIn(binding.step_task_ids["A1"], spawn[2])
        self.assertIn(binding.proof_path, spawn[2])

    def test_sync_maps_task_statuses_into_workflow_and_spawns_structural_reviewer(self):
        binding = self.activate_first()
        first_task = self.tasks.tasks[binding.step_task_ids["A1"]]
        first_task.status = "in_progress"
        method = self.orch.sync_method(self.run.id, "method_01")
        self.assertEqual(method.get_step("A1").status.value, "in_progress")

        self.complete_bound_tasks(binding)
        method = self.orch.sync_method(self.run.id, "method_01")
        self.assertEqual(method.stage, MethodStage.STRUCTURAL_VERIFY)
        reviewer = self.orch.bindings[(self.run.id, "method_01")].structural_reviewer
        self.assertIsNotNone(reviewer)
        self.assertTrue(any(name == reviewer for name, _, _ in self.team.spawned))

    def test_failed_durable_step_fails_method(self):
        binding = self.activate_first()
        self.tasks.tasks[binding.step_task_ids["A1"]].status = "failed"
        method = self.orch.sync_method(self.run.id, "method_01")
        self.assertEqual(method.stage, MethodStage.FAILED)

    def test_structural_done_spawns_detailed_verifier(self):
        self.reach_structural()
        method = self.orch.record_structural_verdict(
            self.run.id,
            "method_01",
            verdict="done",
            issues=[],
        )
        self.assertEqual(method.stage, MethodStage.DETAILED_VERIFY)
        reviewer = self.orch.bindings[(self.run.id, "method_01")].detailed_reviewer
        self.assertTrue(any(name == reviewer for name, _, _ in self.team.spawned))

    def test_structural_continue_spawns_regulator(self):
        self.reach_structural()
        method = self.orch.record_structural_verdict(
            self.run.id,
            "method_01",
            verdict="continue",
            issues=[{"type": "gap", "location": "A3", "description": "missing"}],
        )
        self.assertEqual(method.stage, MethodStage.REGULATION)
        regulator = self.orch.bindings[(self.run.id, "method_01")].regulator_name
        self.assertTrue(any(name == regulator for name, _, _ in self.team.spawned))

    def test_detailed_done_completes_method(self):
        self.reach_structural()
        self.orch.record_structural_verdict(self.run.id, "method_01", verdict="done")
        method = self.orch.record_detailed_verdict(
            self.run.id, "method_01", verdict="done"
        )
        self.assertEqual(method.stage, MethodStage.COMPLETED)

    def test_revise_proof_creates_durable_revision_task_and_returns_to_structural(self):
        binding, _ = self.reach_structural()
        self.orch.record_structural_verdict(
            self.run.id, "method_01", verdict="continue", issues=[]
        )
        method = self.orch.record_regulator_action(
            self.run.id,
            "method_01",
            action="revise_proof",
            notes="repair transversality sign",
        )
        self.assertTrue(method.revision_pending)
        revision_id = binding.revision_task_id
        self.assertIsNotNone(revision_id)
        revision_task = self.tasks.tasks[revision_id]
        self.assertIn("proof_revision", revision_task.tags)
        self.assertIn("repair transversality sign", revision_task.description)
        self.assertEqual(getattr(revision_task, "worktree", None), binding.worktree_name)

        revision_task.status = "completed"
        method = self.orch.sync_method(self.run.id, "method_01")
        self.assertEqual(method.stage, MethodStage.STRUCTURAL_VERIFY)
        self.assertFalse(method.revision_pending)
        self.assertIsNone(binding.revision_task_id)

    def test_revise_plan_returns_to_decomposer_and_new_plan_gets_new_generation(self):
        binding, _ = self.reach_structural()
        self.orch.record_structural_verdict(
            self.run.id, "method_01", verdict="continue", issues=[]
        )
        method = self.orch.record_regulator_action(
            self.run.id,
            "method_01",
            action="revise_plan",
            notes="insert compactness lemma",
        )
        self.assertEqual(method.stage, MethodStage.DECOMPOSITION)
        self.assertEqual(method.steps, [])
        self.assertEqual(binding.step_task_ids, {})
        self.assertTrue(any(name.startswith("decomposer_") for name, _, _ in self.team.spawned))

        updated = self.orch.activate_method(
            self.run.id,
            "method_01",
            steps=[{"id": "B1", "goal": "compactness", "depends_on": []}],
        )
        self.assertEqual(updated.plan_generation, 2)
        task = self.tasks.tasks[updated.step_task_ids["B1"]]
        self.assertIn("plan_generation:2", task.tags)

    def test_dispatch_exposes_one_hard_control_plane_tool(self):
        raw = self.orch.dispatch("start", {"problem_ref": "second_problem.tex"})
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "start")
        bad = json.loads(self.orch.dispatch("skip_all_verification", {}))
        self.assertFalse(bad["ok"])
        self.assertIn("unknown research workflow action", bad["error"])

    def test_dispatch_returns_compact_run_without_abstracts(self):
        self.reach_structural()
        self.orch.record_structural_verdict(
            self.run.id, "method_01", verdict="done"
        )
        self.orch.record_detailed_verdict(
            self.run.id, "method_01", verdict="done"
        )
        large_source = {
            "source_id": "source_large", "title": "Large source",
            "provider": "fixture", "abstract": "X" * 150_000,
        }
        self.run.retrieved_sources.append(large_source)
        self.run.literature_sources.append(large_source)
        raw = self.orch.dispatch(
            "begin_summary",
            {"run_id": self.run.id, "selected_method_ids": ["method_01"]},
        )
        result = json.loads(raw)
        self.assertEqual(result["result"]["stage"], "summary")
        self.assertLess(len(raw), 20_000)
        self.assertNotIn("X" * 100, raw)
        self.assertEqual(self.run.retrieved_sources[-1]["abstract"], "X" * 150_000)

    def test_summary_only_accepts_verified_method(self):
        self.reach_structural()
        self.orch.record_structural_verdict(self.run.id, "method_01", verdict="done")
        self.orch.record_detailed_verdict(self.run.id, "method_01", verdict="done")
        run = self.orch.begin_summary(
            self.run.id, selected_method_ids=["method_01"]
        )
        self.assertEqual(run.stage, ResearchStage.SUMMARY)
        run = self.orch.complete_summary(self.run.id, "proof.md")
        self.assertEqual(run.stage, ResearchStage.COMPLETED)
        self.assertEqual(run.final_artifact, "proof.md")

    def test_status_contains_workflow_and_task_bindings(self):
        binding = self.activate_first()
        status = self.orch.status(self.run.id)
        self.assertEqual(status["run"]["id"], self.run.id)
        self.assertEqual(status["bindings"][0]["method_id"], "method_01")
        self.assertEqual(
            status["bindings"][0]["step_task_ids"], binding.step_task_ids
        )


if __name__ == "__main__":
    unittest.main()
