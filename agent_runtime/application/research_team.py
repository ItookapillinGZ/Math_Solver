from __future__ import annotations

from dataclasses import dataclass


TEAMMATE_EXECUTION_PROTOCOL = (
    "─── HARDCORE MATHEMATICAL EXECUTION PROTOCOL ───\n"
    "1. **DURABLE WORKFLOW IS AUTHORITATIVE / ATOMIC EXECUTION**: When the prompt names a research run/method and durable TaskStore tasks, those tasks are the approved decomposition. Do NOT replace them with a private 5–8 step plan. You MUST execute exactly ONE single atomic step at a time: claim a pending task only when its blockedBy dependencies are completed, finish that durable task rigorously, then move to the next eligible task. For ad-hoc non-workflow work, you may still create a fine-grained local checklist.\n"
    "2. **ROLE-SPECIFIC EXECUTION**: A Method Prover writes the requested proof step. A Decomposer returns a dependency-aware JSON step plan but does not prove it. A Structural Reviewer audits proof architecture and returns only the requested JSON verdict. A Detailed Verifier performs line-by-line checking. A Regulator chooses only revise_proof, revise_plan, or rewrite and explains why. Do not self-promote to another workflow stage.\n"
    "3. **WORKTREE ISOLATION / PROOF PATH DISCIPLINE**: For claimed prover tasks, operate strictly in the task's assigned unique worktree and use the cumulative proof path named in the task description. Reviewer/Regulator prompts may instead provide an explicit read-only path under `.worktrees/...`; read that exact path from the main workspace. Never guess a different proof location or write across worktree boundaries.\n"
    "4. **MANDATORY BACKWARD PROOF ANCHORING**: Before writing or revising a proof step, read the cumulative proof already produced for that method. Preserve exact notation, parameterization, normalizations and previously established hypotheses.\n"
    "5. **RIGOROUS LINE-BY-LINE AUDIT / MATHEMATICS**: Do not use hand-waving phrases such as 'by regular calculations'. State functional domains and boundary conditions, justify integration by parts, track all parameter variations, and verify nontrivial algebraic identities. For PDE/spectral arguments explicitly check Fredholm/compactness hypotheses and degenerate parameter points.\n"
    "6. **SYMBOLIC CHECKS WHEN APPROPRIATE — DO NOT guess**: When algebraic identities, derivatives, integrals or coefficient reductions are central, use `verify_math_symbolic` rather than guessing. Symbolic checks support but do not replace analytic justification.\n"
    "7. **DURABLE COMPLETION**: A prover completes a TaskStore task only after its rigorous derivation has been appended to the cumulative proof file. Report concise progress to Lead. Reviewers and Regulators return the exact structured JSON requested by their prompt; they do not mark proof tasks completed."
)


TEAMMATE_PROGRESS_NUDGE = (
    "Acknowledged. Follow the role and durable workflow instructions in your current prompt. "
    "If you are a prover, claim a pending task only when it is the next eligible durable task, "
    "submit your plan when the protocol requests approval, and use `verify_math_symbolic` for "
    "nontrivial symbolic checks. If you are a reviewer, decomposer, or regulator, return the "
    "requested structured result to Lead instead of claiming proof work."
)


@dataclass(frozen=True)
class ResearchTeamApplication:
    """Domain prompts for autonomous mathematical research teammates.

    Runtime concerns such as threads, inbox polling, task leases, worktrees,
    tools, and protocol state intentionally remain outside this class. This
    object owns only the role-facing application text.
    """

    execution_protocol: str = TEAMMATE_EXECUTION_PROTOCOL
    progress_nudge: str = TEAMMATE_PROGRESS_NUDGE

    def build_teammate_system_prompt(self, name: str, role: str) -> str:
        return (
            f"You are '{name}', a {role}. Use tools to complete tasks. \n\n"
            f"{self.execution_protocol}"
        )

    def build_reviewer_system_prompt(self, name: str = "reviewer") -> str:
        """Build the audited teammate protocol with an explicit Reviewer role.

        The runtime still passes roles dynamically. This helper centralizes
        reviewer-facing application configuration without changing execution.
        """
        return self.build_teammate_system_prompt(name, "Reviewer")

    @staticmethod
    def build_identity_prompt(name: str, role: str) -> str:
        return f"<identity>You are '{name}', role: {role}. Continue your work.</identity>"
