import unittest

from agent_runtime.application.research_team import (
    TEAMMATE_EXECUTION_PROTOCOL,
    TEAMMATE_PROGRESS_NUDGE,
    ResearchTeamApplication,
)


class ResearchTeamApplicationTests(unittest.TestCase):
    def setUp(self):
        self.app = ResearchTeamApplication()

    def test_teammate_prompt_contains_identity_and_role(self):
        prompt = self.app.build_teammate_system_prompt("worker-1", "analyst")
        self.assertIn("You are 'worker-1', a analyst.", prompt)
        self.assertIn("Use tools to complete tasks.", prompt)

    def test_protocol_preserves_symbolic_verification_rule(self):
        prompt = self.app.build_teammate_system_prompt("worker", "researcher")
        self.assertIn("verify_math_symbolic", prompt)
        self.assertIn("DO NOT guess", prompt)

    def test_protocol_preserves_worktree_isolation(self):
        self.assertIn("WORKTREE ISOLATION", TEAMMATE_EXECUTION_PROTOCOL)
        self.assertIn("assigned unique worktree", TEAMMATE_EXECUTION_PROTOCOL)

    def test_protocol_preserves_atomic_proof_workflow(self):
        self.assertIn("exactly ONE single atomic step", TEAMMATE_EXECUTION_PROTOCOL)
        self.assertIn("MANDATORY BACKWARD PROOF ANCHORING", TEAMMATE_EXECUTION_PROTOCOL)
        self.assertIn("LINE-BY-LINE AUDIT", TEAMMATE_EXECUTION_PROTOCOL)

    def test_reviewer_prompt_uses_explicit_reviewer_role(self):
        prompt = self.app.build_reviewer_system_prompt("reviewer-1")
        self.assertIn("You are 'reviewer-1', a Reviewer.", prompt)
        self.assertIn("Reviewer", prompt)
        self.assertIn("verify_math_symbolic", prompt)

    def test_identity_prompt_is_stable(self):
        self.assertEqual(
            self.app.build_identity_prompt("alice", "Reviewer"),
            "<identity>You are 'alice', role: Reviewer. Continue your work.</identity>",
        )

    def test_progress_nudge_keeps_plan_and_verification_requirements(self):
        self.assertIn("claim a pending task", TEAMMATE_PROGRESS_NUDGE)
        self.assertIn("submit your plan", TEAMMATE_PROGRESS_NUDGE)
        self.assertIn("verify_math_symbolic", TEAMMATE_PROGRESS_NUDGE)


if __name__ == "__main__":
    unittest.main()
