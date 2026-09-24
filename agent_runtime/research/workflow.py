from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from .workflow_models import (
    Difficulty,
    MethodAttempt,
    MethodStage,
    ProofStep,
    ProofStepStatus,
    RegulatorAction,
    ResearchRun,
    ResearchStage,
    Verdict,
)


class WorkflowError(RuntimeError):
    """Base error for research-workflow failures."""


class WorkflowTransitionError(WorkflowError):
    """Raised when a caller attempts an illegal workflow transition."""


class WorkflowValidationError(WorkflowError):
    """Raised when structured workflow input is invalid."""


@dataclass(frozen=True)
class MethodProposal:
    name: str
    rationale: str = ""
    source_ids: tuple[str, ...] = ()
    origin: str = "model_reasoning"


@dataclass(frozen=True)
class StepPlan:
    id: str
    goal: str
    depends_on: tuple[str, ...] = ()


class ResearchWorkflowCoordinator:
    """Hard control-flow layer for the mathematical research workflow.

    This class intentionally contains no LLM calls, teammate spawning, TaskStore
    writes, file IO, or tracing.  Step 12A only encodes the legal state graph
    from the research workflow.  Step 12B can bind these transitions to the
    existing teammate/task infrastructure without moving mathematical reasoning
    into the orchestrator.
    """

    def __init__(
        self,
        *,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._run_id_factory = run_id_factory or (
            lambda: f"research_{uuid.uuid4().hex[:12]}"
        )

    def start_run(self, problem_ref: str) -> ResearchRun:
        problem_ref = str(problem_ref).strip()
        if not problem_ref:
            raise WorkflowValidationError("problem_ref cannot be empty")
        return ResearchRun(id=self._run_id_factory(), problem_ref=problem_ref)

    def complete_literature_survey(
        self,
        run: ResearchRun,
        *,
        difficulty: Difficulty | str,
        literature_notes: str = "",
        methods: Sequence[MethodProposal | dict] = (),
    ) -> ResearchRun:
        self._require_run_stage(run, ResearchStage.LITERATURE_SURVEY)
        difficulty_value = self._coerce_enum(Difficulty, difficulty, "difficulty")
        run.difficulty = difficulty_value
        run.literature_notes = str(literature_notes)

        if difficulty_value == Difficulty.EASY:
            if methods:
                raise WorkflowValidationError(
                    "easy branch cannot create method attempts; use direct proof"
                )
            run.stage = ResearchStage.DIRECT_PROOF
            return run

        proposals = [self._coerce_method_proposal(item) for item in methods]
        if not proposals:
            raise WorkflowValidationError(
                "medium/hard literature outcome requires at least one method proposal"
            )
        names = [proposal.name.casefold() for proposal in proposals]
        if len(names) != len(set(names)):
            raise WorkflowValidationError("method proposal names must be unique")

        run.methods = [
            MethodAttempt(
                id=f"method_{index:02d}",
                name=proposal.name,
                rationale=proposal.rationale,
                source_ids=proposal.source_ids,
                origin=proposal.origin,
            )
            for index, proposal in enumerate(proposals, start=1)
        ]
        run.stage = ResearchStage.METHODS_ACTIVE
        return run

    def fail_literature_survey(
        self, run: ResearchRun, status: str, notes: str = ""
    ) -> ResearchRun:
        self._require_run_stage(run, ResearchStage.LITERATURE_SURVEY)
        if status not in {"literature_search_failed", "no_relevant_sources"}:
            raise WorkflowValidationError(f"invalid literature_status: {status}")
        run.literature_status = status
        run.literature_notes = str(notes)
        run.stage = ResearchStage.FAILED
        return run

    def complete_direct_proof(self, run: ResearchRun, artifact_path: str) -> ResearchRun:
        self._require_run_stage(run, ResearchStage.DIRECT_PROOF)
        artifact = str(artifact_path).strip()
        if not artifact:
            raise WorkflowValidationError("artifact_path cannot be empty")
        run.final_artifact = artifact
        run.stage = ResearchStage.COMPLETED
        return run

    def assign_teammate(
        self, run: ResearchRun, method_id: str, teammate: str
    ) -> MethodAttempt:
        method = self._method_for_active_run(run, method_id)
        teammate = str(teammate).strip()
        if not teammate:
            raise WorkflowValidationError("teammate cannot be empty")
        method.teammate = teammate
        return method

    def set_method_plan(
        self,
        run: ResearchRun,
        method_id: str,
        steps: Sequence[StepPlan | dict],
    ) -> MethodAttempt:
        method = self._method_for_active_run(run, method_id)
        self._require_method_stage(method, MethodStage.DECOMPOSITION)
        planned_steps = [self._coerce_step_plan(item) for item in steps]
        self._validate_step_plan(planned_steps)
        method.steps = [
            ProofStep(
                id=step.id,
                goal=step.goal,
                depends_on=tuple(step.depends_on),
            )
            for step in planned_steps
        ]
        method.stage = MethodStage.PROVING
        method.revision_pending = False
        method.structural_verdict = None
        method.detailed_verdict = None
        method.regulator_action = None
        method.regulator_notes = ""
        return method

    def start_step(
        self, run: ResearchRun, method_id: str, step_id: str
    ) -> ProofStep:
        method = self._method_for_active_run(run, method_id)
        self._require_method_stage(method, MethodStage.PROVING)
        if not method.teammate:
            raise WorkflowTransitionError(
                f"method {method.id} must be assigned to a teammate before proof steps start"
            )
        if method.revision_pending:
            raise WorkflowTransitionError(
                f"method {method.id} is waiting for proof revision completion"
            )
        step = self._require_step(method, step_id)
        if step.status != ProofStepStatus.PENDING:
            raise WorkflowTransitionError(
                f"step {step.id} must be pending before it can start"
            )
        incomplete = [
            dep_id
            for dep_id in step.depends_on
            if self._require_step(method, dep_id).status != ProofStepStatus.COMPLETED
        ]
        if incomplete:
            raise WorkflowTransitionError(
                f"step {step.id} is blocked by incomplete dependencies: "
                + ", ".join(incomplete)
            )
        step.status = ProofStepStatus.IN_PROGRESS
        return step

    def complete_step(
        self, run: ResearchRun, method_id: str, step_id: str
    ) -> ProofStep:
        method = self._method_for_active_run(run, method_id)
        self._require_method_stage(method, MethodStage.PROVING)
        step = self._require_step(method, step_id)
        if step.status != ProofStepStatus.IN_PROGRESS:
            raise WorkflowTransitionError(
                f"step {step.id} must be in_progress before completion"
            )
        step.status = ProofStepStatus.COMPLETED
        return step

    def submit_structural_verification(
        self, run: ResearchRun, method_id: str
    ) -> MethodAttempt:
        method = self._method_for_active_run(run, method_id)
        self._require_method_stage(method, MethodStage.PROVING)
        if method.revision_pending:
            raise WorkflowTransitionError(
                f"method {method.id} still has a pending proof revision"
            )
        if not method.all_steps_completed():
            raise WorkflowTransitionError(
                f"method {method.id} cannot enter structural verification "
                "until every proof step is completed"
            )
        method.stage = MethodStage.STRUCTURAL_VERIFY
        return method

    def record_structural_verdict(
        self,
        run: ResearchRun,
        method_id: str,
        verdict: Verdict | str,
    ) -> MethodAttempt:
        method = self._method_for_active_run(run, method_id)
        self._require_method_stage(method, MethodStage.STRUCTURAL_VERIFY)
        value = self._coerce_enum(Verdict, verdict, "verdict")
        method.structural_verdict = value
        method.stage = (
            MethodStage.DETAILED_VERIFY
            if value == Verdict.DONE
            else MethodStage.REGULATION
        )
        return method

    def record_detailed_verdict(
        self,
        run: ResearchRun,
        method_id: str,
        verdict: Verdict | str,
    ) -> MethodAttempt:
        method = self._method_for_active_run(run, method_id)
        self._require_method_stage(method, MethodStage.DETAILED_VERIFY)
        value = self._coerce_enum(Verdict, verdict, "verdict")
        method.detailed_verdict = value
        method.stage = (
            MethodStage.COMPLETED
            if value == Verdict.DONE
            else MethodStage.REGULATION
        )
        return method

    def record_regulator_action(
        self,
        run: ResearchRun,
        method_id: str,
        action: RegulatorAction | str,
        *,
        notes: str = "",
    ) -> MethodAttempt:
        method = self._method_for_active_run(run, method_id)
        self._require_method_stage(method, MethodStage.REGULATION)
        value = self._coerce_enum(RegulatorAction, action, "regulator action")
        method.regulator_action = value
        method.regulator_notes = str(notes)
        method.structural_verdict = None
        method.detailed_verdict = None

        if value == RegulatorAction.REVISE_PROOF:
            method.proof_revision_count += 1
            method.revision_pending = True
            method.stage = MethodStage.PROVING
            return method

        method.plan_revision_count += 1
        if value == RegulatorAction.REWRITE:
            method.rewrite_count += 1
        method.steps = []
        method.revision_pending = False
        method.stage = MethodStage.DECOMPOSITION
        return method

    def complete_proof_revision(
        self, run: ResearchRun, method_id: str
    ) -> MethodAttempt:
        method = self._method_for_active_run(run, method_id)
        self._require_method_stage(method, MethodStage.PROVING)
        if not method.revision_pending:
            raise WorkflowTransitionError(
                f"method {method.id} has no pending proof revision"
            )
        method.revision_pending = False
        method.stage = MethodStage.STRUCTURAL_VERIFY
        return method

    def begin_summary(
        self,
        run: ResearchRun,
        *,
        selected_method_ids: Iterable[str],
    ) -> ResearchRun:
        self._require_run_stage(run, ResearchStage.METHODS_ACTIVE)
        selected = tuple(dict.fromkeys(str(item) for item in selected_method_ids))
        if not selected:
            raise WorkflowValidationError(
                "summary requires at least one selected completed method"
            )
        for method_id in selected:
            method = self._require_method(run, method_id)
            if method.stage != MethodStage.COMPLETED:
                raise WorkflowTransitionError(
                    f"method {method_id} is not fully verified and cannot be summarized"
                )
        run.selected_method_ids = selected
        run.stage = ResearchStage.SUMMARY
        return run

    def complete_summary(self, run: ResearchRun, artifact_path: str) -> ResearchRun:
        self._require_run_stage(run, ResearchStage.SUMMARY)
        artifact = str(artifact_path).strip()
        if not artifact:
            raise WorkflowValidationError("artifact_path cannot be empty")
        run.final_artifact = artifact
        run.stage = ResearchStage.COMPLETED
        return run

    def fail_method(self, run: ResearchRun, method_id: str) -> MethodAttempt:
        method = self._method_for_active_run(run, method_id)
        if method.stage in {
            MethodStage.COMPLETED,
            MethodStage.FAILED,
            MethodStage.ABANDONED,
        }:
            raise WorkflowTransitionError(
                f"method {method.id} is already terminal ({method.stage.value})"
            )
        method.stage = MethodStage.FAILED
        method.revision_pending = False
        return method

    def abandon_method(self, run: ResearchRun, method_id: str) -> MethodAttempt:
        method = self._method_for_active_run(run, method_id)
        if method.stage in {
            MethodStage.COMPLETED,
            MethodStage.FAILED,
            MethodStage.ABANDONED,
        }:
            raise WorkflowTransitionError(
                f"method {method.id} is already terminal ({method.stage.value})"
            )
        method.stage = MethodStage.ABANDONED
        method.revision_pending = False
        return method

    @staticmethod
    def _coerce_enum(enum_cls, value, label: str):
        if isinstance(value, enum_cls):
            return value
        try:
            return enum_cls(str(value).strip().lower())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in enum_cls)
            raise WorkflowValidationError(
                f"invalid {label} {value!r}; expected one of: {allowed}"
            ) from exc

    @staticmethod
    def _coerce_method_proposal(item: MethodProposal | dict) -> MethodProposal:
        if isinstance(item, MethodProposal):
            proposal = item
        elif isinstance(item, dict):
            proposal = MethodProposal(
                name=str(item.get("name", "")),
                rationale=str(item.get("rationale", "")),
                source_ids=tuple(str(x) for x in item.get("source_ids", []) or []),
                origin=str(item.get("origin", "model_reasoning")),
            )
        else:
            raise WorkflowValidationError(
                "method proposals must be MethodProposal or dict values"
            )
        name = proposal.name.strip()
        if not name:
            raise WorkflowValidationError("method proposal name cannot be empty")
        return MethodProposal(name=name, rationale=proposal.rationale.strip(), source_ids=proposal.source_ids, origin=proposal.origin)

    @staticmethod
    def _coerce_step_plan(item: StepPlan | dict) -> StepPlan:
        if isinstance(item, StepPlan):
            step = item
        elif isinstance(item, dict):
            step = StepPlan(
                id=str(item.get("id", "")),
                goal=str(item.get("goal", "")),
                depends_on=tuple(str(dep) for dep in item.get("depends_on", ()) or ()),
            )
        else:
            raise WorkflowValidationError("steps must be StepPlan or dict values")
        step_id = step.id.strip()
        goal = step.goal.strip()
        if not step_id:
            raise WorkflowValidationError("proof step id cannot be empty")
        if not goal:
            raise WorkflowValidationError(f"proof step {step_id} goal cannot be empty")
        return StepPlan(id=step_id, goal=goal, depends_on=tuple(step.depends_on))

    @classmethod
    def _validate_step_plan(cls, steps: Sequence[StepPlan]) -> None:
        if not steps:
            raise WorkflowValidationError("method decomposition requires proof steps")
        ids = [step.id for step in steps]
        if len(ids) != len(set(ids)):
            raise WorkflowValidationError("proof step ids must be unique")
        id_set = set(ids)
        for step in steps:
            if step.id in step.depends_on:
                raise WorkflowValidationError(
                    f"proof step {step.id} cannot depend on itself"
                )
            unknown = [dep for dep in step.depends_on if dep not in id_set]
            if unknown:
                raise WorkflowValidationError(
                    f"proof step {step.id} has unknown dependencies: "
                    + ", ".join(unknown)
                )

        visiting: set[str] = set()
        visited: set[str] = set()
        graph = {step.id: tuple(step.depends_on) for step in steps}

        def visit(step_id: str) -> None:
            if step_id in visited:
                return
            if step_id in visiting:
                raise WorkflowValidationError("proof-step dependency graph contains a cycle")
            visiting.add(step_id)
            for dep in graph[step_id]:
                visit(dep)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in ids:
            visit(step_id)

    @staticmethod
    def _require_run_stage(run: ResearchRun, expected: ResearchStage) -> None:
        if run.stage != expected:
            raise WorkflowTransitionError(
                f"research run {run.id} is in {run.stage.value}; expected {expected.value}"
            )

    @classmethod
    def _method_for_active_run(cls, run: ResearchRun, method_id: str) -> MethodAttempt:
        cls._require_run_stage(run, ResearchStage.METHODS_ACTIVE)
        return cls._require_method(run, method_id)

    @staticmethod
    def _require_method(run: ResearchRun, method_id: str) -> MethodAttempt:
        method = run.get_method(str(method_id))
        if method is None:
            raise WorkflowValidationError(f"unknown method id: {method_id}")
        return method

    @staticmethod
    def _require_method_stage(method: MethodAttempt, expected: MethodStage) -> None:
        if method.stage != expected:
            raise WorkflowTransitionError(
                f"method {method.id} is in {method.stage.value}; expected {expected.value}"
            )

    @staticmethod
    def _require_step(method: MethodAttempt, step_id: str) -> ProofStep:
        step = method.get_step(str(step_id))
        if step is None:
            raise WorkflowValidationError(
                f"unknown proof step {step_id!r} for method {method.id}"
            )
        return step
