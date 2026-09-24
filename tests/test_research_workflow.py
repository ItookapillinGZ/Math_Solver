import unittest

from agent_runtime.research import (
    Difficulty,
    MethodStage,
    ProofStepStatus,
    RegulatorAction,
    ResearchStage,
    ResearchWorkflowCoordinator,
    Verdict,
    WorkflowTransitionError,
    WorkflowValidationError,
)


class ResearchWorkflowCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.workflow = ResearchWorkflowCoordinator(
            run_id_factory=lambda: "research_test"
        )

    def medium_run(self):
        run = self.workflow.start_run("problem.tex")
        self.workflow.complete_literature_survey(
            run,
            difficulty=Difficulty.MEDIUM,
            literature_notes="Two plausible proof strategies.",
            methods=[
                {"name": "bifurcation", "rationale": "Use kernel structure."},
                {"name": "energy", "rationale": "Try a priori bounds."},
            ],
        )
        return run

    def planned_method(self):
        run = self.medium_run()
        self.workflow.assign_teammate(run, "method_01", "proof_worker_a")
        self.workflow.set_method_plan(
            run,
            "method_01",
            [
                {"id": "A1", "goal": "Establish Fredholm property."},
                {
                    "id": "A2",
                    "goal": "Identify the kernel.",
                    "depends_on": ["A1"],
                },
                {
                    "id": "A3",
                    "goal": "Verify transversality.",
                    "depends_on": ["A2"],
                },
            ],
        )
        return run

    def complete_steps(self, run, method_id="method_01"):
        for step_id in ("A1", "A2", "A3"):
            self.workflow.start_step(run, method_id, step_id)
            self.workflow.complete_step(run, method_id, step_id)

    def test_new_run_starts_at_literature_survey(self):
        run = self.workflow.start_run("problem.tex")
        self.assertEqual(run.id, "research_test")
        self.assertEqual(run.stage, ResearchStage.LITERATURE_SURVEY)
        self.assertEqual(run.problem_ref, "problem.tex")

    def test_easy_branch_goes_to_direct_proof_and_completes(self):
        run = self.workflow.start_run("problem.tex")
        self.workflow.complete_literature_survey(run, difficulty="easy")
        self.assertEqual(run.stage, ResearchStage.DIRECT_PROOF)
        self.workflow.complete_direct_proof(run, "proof.md")
        self.assertEqual(run.stage, ResearchStage.COMPLETED)
        self.assertEqual(run.final_artifact, "proof.md")

    def test_medium_hard_branch_requires_methods(self):
        run = self.workflow.start_run("problem.tex")
        with self.assertRaises(WorkflowValidationError):
            self.workflow.complete_literature_survey(run, difficulty="hard")

    def test_medium_branch_creates_independent_method_attempts(self):
        run = self.medium_run()
        self.assertEqual(run.stage, ResearchStage.METHODS_ACTIVE)
        self.assertEqual([m.id for m in run.methods], ["method_01", "method_02"])
        self.assertTrue(all(m.stage == MethodStage.DECOMPOSITION for m in run.methods))
        self.workflow.assign_teammate(run, "method_01", "proof_worker_a")
        self.workflow.assign_teammate(run, "method_02", "proof_worker_b")
        self.assertEqual(run.methods[0].teammate, "proof_worker_a")
        self.assertEqual(run.methods[1].teammate, "proof_worker_b")

    def test_decomposition_rejects_unknown_dependency_and_cycle(self):
        run = self.medium_run()
        with self.assertRaises(WorkflowValidationError):
            self.workflow.set_method_plan(
                run,
                "method_01",
                [{"id": "A1", "goal": "x", "depends_on": ["missing"]}],
            )
        with self.assertRaises(WorkflowValidationError):
            self.workflow.set_method_plan(
                run,
                "method_01",
                [
                    {"id": "A1", "goal": "x", "depends_on": ["A2"]},
                    {"id": "A2", "goal": "y", "depends_on": ["A1"]},
                ],
            )

    def test_proof_step_cannot_start_before_method_has_teammate(self):
        run = self.medium_run()
        self.workflow.set_method_plan(
            run,
            "method_01",
            [{"id": "A1", "goal": "Establish the first lemma."}],
        )
        with self.assertRaises(WorkflowTransitionError):
            self.workflow.start_step(run, "method_01", "A1")
        self.workflow.assign_teammate(run, "method_01", "proof_worker_a")
        step = self.workflow.start_step(run, "method_01", "A1")
        self.assertEqual(step.status, ProofStepStatus.IN_PROGRESS)

    def test_step_dependencies_are_hard_gates(self):
        run = self.planned_method()
        method = run.get_method("method_01")
        with self.assertRaises(WorkflowTransitionError):
            self.workflow.start_step(run, "method_01", "A2")
        self.workflow.start_step(run, "method_01", "A1")
        self.workflow.complete_step(run, "method_01", "A1")
        self.workflow.start_step(run, "method_01", "A2")
        self.assertEqual(method.get_step("A2").status, ProofStepStatus.IN_PROGRESS)

    def test_structural_verification_requires_all_steps_completed(self):
        run = self.planned_method()
        with self.assertRaises(WorkflowTransitionError):
            self.workflow.submit_structural_verification(run, "method_01")
        self.complete_steps(run)
        method = self.workflow.submit_structural_verification(run, "method_01")
        self.assertEqual(method.stage, MethodStage.STRUCTURAL_VERIFY)

    def test_structural_done_goes_to_detailed_and_cannot_skip_to_summary(self):
        run = self.planned_method()
        self.complete_steps(run)
        self.workflow.submit_structural_verification(run, "method_01")
        method = self.workflow.record_structural_verdict(
            run, "method_01", Verdict.DONE
        )
        self.assertEqual(method.stage, MethodStage.DETAILED_VERIFY)
        with self.assertRaises(WorkflowTransitionError):
            self.workflow.begin_summary(run, selected_method_ids=["method_01"])

    def test_structural_continue_routes_to_regulator_and_revise_proof_loop(self):
        run = self.planned_method()
        self.complete_steps(run)
        self.workflow.submit_structural_verification(run, "method_01")
        self.workflow.record_structural_verdict(run, "method_01", Verdict.CONTINUE)
        method = self.workflow.record_regulator_action(
            run,
            "method_01",
            RegulatorAction.REVISE_PROOF,
            notes="Repair transversality argument.",
        )
        self.assertEqual(method.stage, MethodStage.PROVING)
        self.assertTrue(method.revision_pending)
        self.assertEqual(method.proof_revision_count, 1)
        with self.assertRaises(WorkflowTransitionError):
            self.workflow.submit_structural_verification(run, "method_01")
        self.workflow.complete_proof_revision(run, "method_01")
        self.assertEqual(method.stage, MethodStage.STRUCTURAL_VERIFY)

    def test_detailed_continue_revise_plan_returns_to_decomposition(self):
        run = self.planned_method()
        self.complete_steps(run)
        self.workflow.submit_structural_verification(run, "method_01")
        self.workflow.record_structural_verdict(run, "method_01", "done")
        self.workflow.record_detailed_verdict(run, "method_01", "continue")
        method = self.workflow.record_regulator_action(
            run, "method_01", "revise_plan"
        )
        self.assertEqual(method.stage, MethodStage.DECOMPOSITION)
        self.assertEqual(method.steps, [])
        self.assertEqual(method.plan_revision_count, 1)

    def test_rewrite_returns_to_decomposition_and_counts_rewrite(self):
        run = self.planned_method()
        self.complete_steps(run)
        self.workflow.submit_structural_verification(run, "method_01")
        self.workflow.record_structural_verdict(run, "method_01", "continue")
        method = self.workflow.record_regulator_action(
            run, "method_01", "rewrite"
        )
        self.assertEqual(method.stage, MethodStage.DECOMPOSITION)
        self.assertEqual(method.rewrite_count, 1)
        self.assertEqual(method.plan_revision_count, 1)

    def test_detailed_done_completes_method_but_not_whole_research_run(self):
        run = self.planned_method()
        self.complete_steps(run)
        self.workflow.submit_structural_verification(run, "method_01")
        self.workflow.record_structural_verdict(run, "method_01", "done")
        method = self.workflow.record_detailed_verdict(run, "method_01", "done")
        self.assertEqual(method.stage, MethodStage.COMPLETED)
        self.assertEqual(run.stage, ResearchStage.METHODS_ACTIVE)

    def test_summary_requires_completed_selected_method(self):
        run = self.medium_run()
        with self.assertRaises(WorkflowTransitionError):
            self.workflow.begin_summary(run, selected_method_ids=["method_01"])

        self.workflow.assign_teammate(run, "method_01", "proof_worker_a")
        self.workflow.set_method_plan(
            run,
            "method_01",
            [{"id": "A1", "goal": "Finish proof."}],
        )
        self.workflow.start_step(run, "method_01", "A1")
        self.workflow.complete_step(run, "method_01", "A1")
        self.workflow.submit_structural_verification(run, "method_01")
        self.workflow.record_structural_verdict(run, "method_01", "done")
        self.workflow.record_detailed_verdict(run, "method_01", "done")

        self.workflow.begin_summary(run, selected_method_ids=["method_01"])
        self.assertEqual(run.stage, ResearchStage.SUMMARY)
        self.assertEqual(run.selected_method_ids, ("method_01",))
        self.workflow.complete_summary(run, "proof.md")
        self.assertEqual(run.stage, ResearchStage.COMPLETED)
        self.assertEqual(run.final_artifact, "proof.md")

    def test_terminal_method_cannot_be_failed_twice(self):
        run = self.medium_run()
        method = self.workflow.fail_method(run, "method_01")
        self.assertEqual(method.stage, MethodStage.FAILED)
        with self.assertRaises(WorkflowTransitionError):
            self.workflow.fail_method(run, "method_01")

    def test_models_are_json_ready_through_to_dict(self):
        run = self.medium_run()
        payload = run.to_dict()
        self.assertEqual(payload["stage"], "methods_active")
        self.assertEqual(payload["difficulty"], "medium")
        self.assertEqual(payload["methods"][0]["stage"], "decomposition")


if __name__ == "__main__":
    unittest.main()
