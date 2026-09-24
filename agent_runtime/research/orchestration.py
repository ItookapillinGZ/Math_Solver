from __future__ import annotations

import json
from pathlib import Path
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .literature import classify_literature_result
from .literature_search import BUILTIN_PROVIDERS, deduplicate_sources
from .workflow import (
    MethodProposal,
    ResearchWorkflowCoordinator,
    StepPlan,
    WorkflowError,
    WorkflowTransitionError,
    WorkflowValidationError,
)
from .workflow_models import (
    MethodAttempt,
    MethodStage,
    ProofStepStatus,
    RegulatorAction,
    ResearchRun,
    ResearchStage,
    Verdict,
)


@dataclass(frozen=True)
class ResearchWorkflowOrchestratorDependencies:
    """Concrete runtime hooks used by the workflow integration layer.

    The orchestrator intentionally depends on narrow callables rather than the
    concrete TaskRuntime/TeammateRuntime classes.  That keeps the mathematical
    workflow testable without starting model threads or opening SQLite stores.
    """

    create_task: Callable[..., Any]
    load_task: Callable[[str], Any]
    spawn_teammate: Callable[[str, str, str], str]
    send_message: Callable[[str, str], str]
    create_worktree: Callable[[str, str], str] | None = None
    bind_task_to_worktree: Callable[[str, str], Any] | None = None
    terminal_print: Callable[[str], None] = print
    record_event: Callable[[str, str, Mapping[str, Any]], None] | None = None
    save_snapshot: Callable[[str, Mapping[str, Any], list[Mapping[str, Any]]], None] | None = None
    load_snapshots: Callable[[], list[Mapping[str, Any]]] | None = None
    allow_offline_literature_fixture: bool = False


@dataclass
class MethodExecutionBinding:
    run_id: str
    method_id: str
    prover_name: str | None = None
    prover_role: str | None = None
    proof_path: str = ""
    worktree_name: str | None = None
    plan_generation: int = 0
    step_task_ids: dict[str, str] = field(default_factory=dict)
    step_order: tuple[str, ...] = ()
    revision_task_id: str | None = None
    structural_reviewer: str | None = None
    detailed_reviewer: str | None = None
    regulator_name: str | None = None

    def review_path(self) -> str:
        if not self.worktree_name:
            return self.proof_path
        return f".worktrees/{self.worktree_name}/{self.proof_path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "method_id": self.method_id,
            "prover_name": self.prover_name,
            "prover_role": self.prover_role,
            "proof_path": self.proof_path,
            "worktree_name": self.worktree_name,
            "review_path": self.review_path(),
            "plan_generation": self.plan_generation,
            "step_task_ids": dict(self.step_task_ids),
            "step_order": list(self.step_order),
            "revision_task_id": self.revision_task_id,
            "structural_reviewer": self.structural_reviewer,
            "detailed_reviewer": self.detailed_reviewer,
            "regulator_name": self.regulator_name,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MethodExecutionBinding":
        return cls(
            run_id=str(data.get("run_id", "")),
            method_id=str(data.get("method_id", "")),
            prover_name=(str(data["prover_name"]) if data.get("prover_name") is not None else None),
            prover_role=(str(data["prover_role"]) if data.get("prover_role") is not None else None),
            proof_path=str(data.get("proof_path", "")),
            worktree_name=(str(data["worktree_name"]) if data.get("worktree_name") is not None else None),
            plan_generation=int(data.get("plan_generation") or 0),
            step_task_ids={str(k): str(v) for k, v in dict(data.get("step_task_ids") or {}).items()},
            step_order=tuple(str(item) for item in data.get("step_order", ()) or ()),
            revision_task_id=(str(data["revision_task_id"]) if data.get("revision_task_id") is not None else None),
            structural_reviewer=(str(data["structural_reviewer"]) if data.get("structural_reviewer") is not None else None),
            detailed_reviewer=(str(data["detailed_reviewer"]) if data.get("detailed_reviewer") is not None else None),
            regulator_name=(str(data["regulator_name"]) if data.get("regulator_name") is not None else None),
        )


class ResearchWorkflowOrchestrator:
    """Bind the hard workflow state machine to durable tasks and teammates.

    Step 12B deliberately keeps mathematical interpretation in the LLM layer.
    Decomposers, reviewers and regulators are spawned automatically, but their
    structured JSON is only *submitted* to this orchestrator by the Lead.  The
    orchestrator owns hard transitions, task dependencies, teammate affinity,
    and the routing loops back to proof/decomposition.
    """

    ACTIONS = (
        "start",
        "literature",
        "request_decomposition",
        "activate_method",
        "sync_method",
        "structural_verdict",
        "detailed_verdict",
        "regulator",
        "begin_summary",
        "complete_summary",
        "complete_direct_proof",
        "status",
    )

    def __init__(
        self,
        workflow: ResearchWorkflowCoordinator,
        dependencies: ResearchWorkflowOrchestratorDependencies,
    ) -> None:
        self.workflow = workflow
        self.deps = dependencies
        self.runs: dict[str, ResearchRun] = {}
        self.bindings: dict[tuple[str, str], MethodExecutionBinding] = {}
        self._restore_persisted_state()

    # ------------------------------------------------------------------ runs
    def start_run(self, problem_ref: str) -> ResearchRun:
        run = self.workflow.start_run(problem_ref)
        self.runs[run.id] = run
        self.deps.terminal_print(
            f"  [research] started {run.id}: {run.problem_ref}"
        )
        return run

    def complete_literature_survey(
        self,
        run_id: str,
        *,
        difficulty: str,
        literature_notes: str = "",
        methods: Sequence[MethodProposal | Mapping[str, Any]] = (),
        selected_sources: Sequence[Mapping[str, Any]] = (),
        auto_decompose: bool = True,
        literature_status: str = "completed",
        offline_fixture: bool = False,
    ) -> ResearchRun:
        run = self._require_run(run_id)
        if run.stage != ResearchStage.LITERATURE_SURVEY:
            raise WorkflowTransitionError(
                f"research run {run.id} is in {run.stage.value}; expected literature_survey"
            )
        status = str(literature_status).strip().lower()
        if status in {"literature_search_failed", "no_relevant_sources"}:
            status = "degraded"
        if status not in {"completed", "degraded"}:
            raise WorkflowValidationError(f"invalid literature_status: {literature_status}")
        selected: list[dict[str, str]] = []
        if offline_fixture:
            if not self.deps.allow_offline_literature_fixture:
                raise WorkflowValidationError("offline literature fixture is not enabled")
        elif status == "completed":
            if not any(event["status"] == "success" and event["sources"] for event in run.literature_retrievals):
                raise WorkflowValidationError("literature completion requires successful retrieval evidence")
            if not str(literature_notes).strip():
                raise WorkflowValidationError("literature completion requires literature_notes")
            by_id = {source["source_id"]: source for source in run.retrieved_sources}
            for item in selected_sources:
                source_id = str(item.get("source_id", "")).strip()
                note = str(item.get("relevance_note", "")).strip()
                source = by_id.get(source_id)
                if source is None or not note:
                    raise WorkflowValidationError("selected source requires a retrieved source_id and relevance_note")
                if source_id not in {entry["source_id"] for entry in selected}:
                    selected.append({
                        "source_id": source_id, "title": source["title"],
                        "relevance_note": note,
                    })
            if not selected:
                raise WorkflowValidationError("literature completion requires at least one selected source")
        else:
            if not run.literature_retrievals or not all(
                event["status"] in {"failed", "no_results"} for event in run.literature_retrievals
            ):
                raise WorkflowValidationError(
                    "degraded literature requires actual failed or zero-result retrieval attempts"
                )
            if selected_sources:
                raise WorkflowValidationError("degraded literature cannot select unavailable sources")
            literature_notes = (
                "Literature retrieval unavailable / degraded. " + str(literature_notes).strip()
            ).strip()
        selected_ids = {item["source_id"] for item in selected}
        for method in methods:
            source_ids = (
                method.source_ids if isinstance(method, MethodProposal)
                else tuple(str(x) for x in method.get("source_ids", []) or [])
            )
            origin = (
                method.origin if isinstance(method, MethodProposal)
                else str(method.get("origin", "model_reasoning"))
            )
            if origin not in {"model_reasoning", "literature"}:
                raise WorkflowValidationError(f"invalid method origin: {origin}")
            if source_ids and not set(source_ids).issubset(selected_ids):
                raise WorkflowValidationError("method source_ids must refer to selected sources")
            if origin == "literature" and not source_ids:
                raise WorkflowValidationError("literature-origin method requires source_ids")
        self.workflow.complete_literature_survey(
            run, difficulty=difficulty, literature_notes=literature_notes, methods=methods,
        )
        run.selected_sources = selected
        for source in selected:
            self._literature_event(run, "literature.source_selected", {
                "source_id": source["source_id"], "title": source["title"],
                "relevance_note": source["relevance_note"],
            })
        run.literature_status = "offline_fixture" if offline_fixture else status
        self._literature_event(run, "literature.completed" if status == "completed" else "literature.degraded", {
            "status": run.literature_status,
            "selected_source_ids": [item["source_id"] for item in selected],
        })
        if run.stage == ResearchStage.METHODS_ACTIVE and auto_decompose:
            for method in run.methods:
                self.request_decomposition(run.id, method.id)
        return run

    def record_literature_query_started(
        self, run_id: str, tool_name: str, tool_input: Mapping[str, Any]
    ) -> None:
        run = self._require_run(run_id)
        if run.stage != ResearchStage.LITERATURE_SURVEY:
            return
        providers = list(BUILTIN_PROVIDERS) if tool_name == "search_literature" else ["external_mcp:docs"]
        self._literature_event(run, "literature.query_started", {
            "query": str(tool_input.get("query") or "").strip(),
            "providers": providers,
        })

    def record_literature_tool_result(
        self,
        run_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
        output: str,
    ) -> dict[str, Any] | None:
        run = self._require_run(run_id)
        if run.stage != ResearchStage.LITERATURE_SURVEY:
            return None
        event = classify_literature_result(
            tool_name, tool_input, output,
            latency_ms=float(tool_input.get("__latency_ms", 0) or 0),
        )
        if event is None:
            return None
        run.literature_retrievals.append(event)
        combined = deduplicate_sources([*run.retrieved_sources, *event["sources"]])
        run.retrieved_sources = combined
        run.literature_sources = list(combined)
        statuses = event.get("provider_statuses") or {
            event["provider"]: {
                "status": event["status"], "result_count": event["result_count"],
                "latency_ms": event["latency_ms"], "error_type": event["error_type"],
                "message": event["message"],
            }
        }
        for provider, details in statuses.items():
            payload = {
                "query": event["query"], "provider": provider,
                "status": details.get("status", "failed"),
                "result_count": int(details.get("result_count") or 0),
                "latency_ms": float(details.get("latency_ms") or 0),
            }
            if payload["status"] == "failed":
                payload.update(error_type=str(details.get("error_type") or "provider_error"),
                               message=str(details.get("message") or "")[:200])
                self._literature_event(run, "literature.provider_failed", payload)
            else:
                self._literature_event(run, "literature.provider_completed", payload)
        summary = {
            "query": event["query"], "provider": event["provider"],
            "status": event["status"], "result_count": event["result_count"],
            "raw_result_count": event["raw_result_count"],
            "deduplicated_result_count": event["deduplicated_result_count"],
            "latency_ms": event["latency_ms"],
        }
        if event["status"] == "failed":
            summary.update(error_type=event["error_type"], message=event["message"][:200])
            self._literature_event(run, "literature.query_failed", summary)
        self._literature_event(run, "literature.query_completed", summary)
        self._save_run_snapshot(run.id)
        return event

    def _literature_event(self, run: ResearchRun, kind: str, payload: Mapping[str, Any]) -> None:
        if self.deps.record_event is None:
            return
        try:
            self.deps.record_event(kind, run.id, payload)
        except Exception as exc:
            self.deps.terminal_print(f"  [research trace warning] {kind} save failed: {exc}")

    # ------------------------------------------------------------- decomposition
    def request_decomposition(
        self,
        run_id: str,
        method_id: str,
        *,
        teammate_name: str | None = None,
    ) -> str:
        run = self._require_run(run_id)
        method = self._require_method(run, method_id)
        if run.stage != ResearchStage.METHODS_ACTIVE:
            raise WorkflowTransitionError(
                f"research run {run.id} is in {run.stage.value}; methods are not active"
            )
        if method.stage != MethodStage.DECOMPOSITION:
            raise WorkflowTransitionError(
                f"method {method.id} is in {method.stage.value}; decomposition is not allowed"
            )

        name = teammate_name or f"decomposer_{self._safe_id(run.id)}_{method.id}"
        role = f"Mathematical Decomposer {run.id} {method.id}"
        prompt = self._decomposition_prompt(run, method)
        return self._spawn_or_wake(name, role, prompt)

    def activate_method(
        self,
        run_id: str,
        method_id: str,
        *,
        steps: Sequence[StepPlan | Mapping[str, Any]],
        teammate_name: str | None = None,
    ) -> MethodExecutionBinding:
        run = self._require_run(run_id)
        method = self._require_method(run, method_id)
        self.workflow.set_method_plan(run, method_id, steps)

        binding = self.bindings.get((run.id, method.id))
        if binding is None:
            binding = MethodExecutionBinding(
                run_id=run.id,
                method_id=method.id,
                proof_path=f"PROOF/{run.id}/{method.id}/proof.md",
            )
            self.bindings[(run.id, method.id)] = binding

        binding.plan_generation += 1
        binding.step_task_ids = {}
        binding.revision_task_id = None
        ordered_step_ids = self._topological_step_ids(method)
        binding.step_order = tuple(ordered_step_ids)

        prover_name = teammate_name or binding.prover_name or (
            f"prover_{self._safe_id(run.id)}_{method.id}"
        )
        prover_role = f"Mathematical Researcher {run.id} {method.id}"
        binding.prover_name = prover_name
        binding.prover_role = prover_role

        # Create durable tasks in topological order so every blockedBy reference
        # points to a task that already exists in TaskStore.
        for step_id in ordered_step_ids:
            step = method.get_step(step_id)
            assert step is not None
            blocked_by = [binding.step_task_ids[dep] for dep in step.depends_on]
            task = self.deps.create_task(
                subject=f"[{run.id}/{method.id}] {step.id}: {step.goal}",
                description=self._step_task_description(run, method, step.id, step.goal, binding),
                blockedBy=blocked_by,
                task_type="math",
                required_roles=[prover_role],
                tags=[
                    "research_workflow",
                    f"research_run:{run.id}",
                    f"method:{method.id}",
                    f"step:{step.id}",
                    f"plan_generation:{binding.plan_generation}",
                ],
            )
            binding.step_task_ids[step.id] = str(task.id)

        self._ensure_method_worktree(binding)

        self.workflow.assign_teammate(run, method.id, prover_name)
        prompt = self._prover_prompt(run, method, binding)
        self._spawn_or_wake(prover_name, prover_role, prompt)
        return binding

    # ------------------------------------------------------------------- sync
    def sync_method(self, run_id: str, method_id: str) -> MethodAttempt:
        run = self._require_run(run_id)
        method = self._require_method(run, method_id)
        binding = self._require_binding(run.id, method.id)

        if method.stage in {MethodStage.FAILED, MethodStage.ABANDONED, MethodStage.COMPLETED}:
            return method

        if method.revision_pending:
            self._sync_revision_task(run, method, binding)
            return method

        if method.stage != MethodStage.PROVING:
            return method

        for step_id in binding.step_order:
            task_id = binding.step_task_ids.get(step_id)
            if not task_id:
                raise WorkflowValidationError(
                    f"missing durable task binding for {run.id}/{method.id}/{step_id}"
                )
            task = self.deps.load_task(task_id)
            step = method.get_step(step_id)
            if step is None:
                raise WorkflowValidationError(
                    f"workflow step disappeared: {run.id}/{method.id}/{step_id}"
                )

            status = str(task.status)
            if status in {"failed", "cancelled"}:
                self.workflow.fail_method(run, method.id)
                self.deps.terminal_print(
                    f"  [research] {method.id} failed because task {task_id} is {status}"
                )
                return method
            if status == "in_progress" and step.status == ProofStepStatus.PENDING:
                self.workflow.start_step(run, method.id, step.id)
            if status == "completed":
                if step.status == ProofStepStatus.PENDING:
                    self.workflow.start_step(run, method.id, step.id)
                if step.status == ProofStepStatus.IN_PROGRESS:
                    self.workflow.complete_step(run, method.id, step.id)

        if method.stage == MethodStage.PROVING and method.all_steps_completed():
            self.workflow.submit_structural_verification(run, method.id)
            self._spawn_structural_reviewer(run, method, binding)
        return method

    # --------------------------------------------------------------- reviewers
    def record_structural_verdict(
        self,
        run_id: str,
        method_id: str,
        *,
        verdict: str,
        issues: Sequence[Mapping[str, Any]] = (),
    ) -> MethodAttempt:
        run = self._require_run(run_id)
        method = self.workflow.record_structural_verdict(run, method_id, verdict)
        binding = self._require_binding(run.id, method.id)
        if method.stage == MethodStage.DETAILED_VERIFY:
            self._spawn_detailed_reviewer(run, method, binding, issues)
        elif method.stage == MethodStage.REGULATION:
            self._spawn_regulator(run, method, binding, "structural", issues)
        return method

    def record_detailed_verdict(
        self,
        run_id: str,
        method_id: str,
        *,
        verdict: str,
        issues: Sequence[Mapping[str, Any]] = (),
    ) -> MethodAttempt:
        run = self._require_run(run_id)
        method = self.workflow.record_detailed_verdict(run, method_id, verdict)
        binding = self._require_binding(run.id, method.id)
        if method.stage == MethodStage.REGULATION:
            self._spawn_regulator(run, method, binding, "detailed", issues)
        return method

    def record_regulator_action(
        self,
        run_id: str,
        method_id: str,
        *,
        action: str,
        notes: str = "",
    ) -> MethodAttempt:
        run = self._require_run(run_id)
        method = self.workflow.record_regulator_action(
            run,
            method_id,
            action,
            notes=notes,
        )
        binding = self._require_binding(run.id, method.id)

        if method.regulator_action == RegulatorAction.REVISE_PROOF:
            task = self.deps.create_task(
                subject=f"[{run.id}/{method.id}] revise proof after verifier feedback",
                description=(
                    f"Revise the cumulative proof at {binding.proof_path}.\n"
                    f"Regulator instructions:\n{notes or '(no additional notes)'}\n"
                    "Do not change the approved decomposition unless the workflow is "
                    "explicitly moved to REVISE_PLAN. Complete this task when the proof "
                    "has been patched and rechecked."
                ),
                blockedBy=[],
                task_type="math",
                required_roles=[binding.prover_role] if binding.prover_role else [],
                tags=[
                    "research_workflow",
                    f"research_run:{run.id}",
                    f"method:{method.id}",
                    "proof_revision",
                    f"revision:{method.proof_revision_count}",
                ],
            )
            binding.revision_task_id = str(task.id)
            if binding.worktree_name and self.deps.bind_task_to_worktree is not None:
                self.deps.bind_task_to_worktree(binding.revision_task_id, binding.worktree_name)
            if binding.prover_name and binding.prover_role:
                self._spawn_or_wake(
                    binding.prover_name,
                    binding.prover_role,
                    (
                        f"The regulator requires a proof revision for {method.id}. "
                        f"Claim durable task {task.id}. Instructions: {notes}"
                    ),
                )
            return method

        # REVISE_PLAN and REWRITE both return to the hard decomposition gate.
        binding.step_task_ids = {}
        binding.step_order = ()
        binding.revision_task_id = None
        self.request_decomposition(run.id, method.id)
        return method

    # ---------------------------------------------------------------- summary
    def begin_summary(
        self,
        run_id: str,
        *,
        selected_method_ids: Sequence[str],
    ) -> ResearchRun:
        run = self._require_run(run_id)
        return self.workflow.begin_summary(
            run,
            selected_method_ids=selected_method_ids,
        )

    def complete_summary(self, run_id: str, artifact_path: str) -> ResearchRun:
        run = self._require_run(run_id)
        self._annotate_degraded_artifact(run, artifact_path)
        return self.workflow.complete_summary(run, artifact_path)

    def complete_direct_proof(self, run_id: str, artifact_path: str) -> ResearchRun:
        run = self._require_run(run_id)
        self._annotate_degraded_artifact(run, artifact_path)
        return self.workflow.complete_direct_proof(run, artifact_path)

    @staticmethod
    def _annotate_degraded_artifact(run: ResearchRun, artifact_path: str) -> None:
        if run.literature_status != "degraded":
            return
        path = Path(artifact_path)
        if not path.is_file():
            raise WorkflowValidationError("degraded literature requires an existing final artifact")
        notice = (
            "> Literature retrieval unavailable / degraded. No literature "
            "citations were verified for this research run.\n\n"
        )
        content = path.read_text(encoding="utf-8")
        if notice not in content:
            path.write_text(notice + content, encoding="utf-8")

    # ------------------------------------------------------------- tool facade
    def dispatch(self, action: str, payload: Mapping[str, Any] | None = None) -> str:
        """Single tool-facing entrypoint with hard action validation."""
        try:
            data = dict(payload or {})
            action_value = str(action).strip().lower()
            supplied_id = str(data.get("run_id", "")).strip()
            safe_id = (
                supplied_id if re.fullmatch(r"(?:research|benchmark)_[A-Za-z0-9_-]{1,64}", supplied_id)
                else "(invalid or absent)"
            )
            safe_action = action_value if action_value in self.ACTIONS else "(invalid)"
            self.deps.terminal_print(
                f"  [research workflow] action={safe_action} "
                f"run_id_present={bool(supplied_id)} run_id={safe_id}"
            )
            if action_value not in self.ACTIONS:
                raise WorkflowValidationError(
                    f"unknown research workflow action {action!r}; expected one of: "
                    + ", ".join(self.ACTIONS)
                )

            if action_value == "start":
                result: Any = self.start_run(str(data.get("problem_ref", "")))
            elif action_value == "literature":
                result = self.complete_literature_survey(
                    str(data.get("run_id", "")),
                    difficulty=str(data.get("difficulty", "")),
                    literature_notes=str(data.get("literature_notes", "")),
                    methods=data.get("methods", ()) or (),
                    selected_sources=data.get("selected_sources", ()) or (),
                    auto_decompose=bool(data.get("auto_decompose", True)),
                    literature_status=str(data.get("literature_status", "completed")),
                    offline_fixture=bool(data.get("offline_fixture", False)),
                )
            elif action_value == "request_decomposition":
                result = self.request_decomposition(
                    str(data.get("run_id", "")),
                    str(data.get("method_id", "")),
                    teammate_name=(
                        str(data["teammate_name"])
                        if data.get("teammate_name") is not None
                        else None
                    ),
                )
            elif action_value == "activate_method":
                result = self.activate_method(
                    str(data.get("run_id", "")),
                    str(data.get("method_id", "")),
                    steps=data.get("steps", ()) or (),
                    teammate_name=(
                        str(data["teammate_name"])
                        if data.get("teammate_name") is not None
                        else None
                    ),
                )
            elif action_value == "sync_method":
                result = self.sync_method(
                    str(data.get("run_id", "")),
                    str(data.get("method_id", "")),
                )
            elif action_value == "structural_verdict":
                result = self.record_structural_verdict(
                    str(data.get("run_id", "")),
                    str(data.get("method_id", "")),
                    verdict=str(data.get("verdict", "")),
                    issues=data.get("issues", ()) or (),
                )
            elif action_value == "detailed_verdict":
                result = self.record_detailed_verdict(
                    str(data.get("run_id", "")),
                    str(data.get("method_id", "")),
                    verdict=str(data.get("verdict", "")),
                    issues=data.get("issues", ()) or (),
                )
            elif action_value == "regulator":
                result = self.record_regulator_action(
                    str(data.get("run_id", "")),
                    str(data.get("method_id", "")),
                    action=str(data.get("regulator_action", "")),
                    notes=str(data.get("notes", "")),
                )
            elif action_value == "begin_summary":
                result = self.begin_summary(
                    str(data.get("run_id", "")),
                    selected_method_ids=data.get("selected_method_ids", ()) or (),
                )
            elif action_value == "complete_summary":
                result = self.complete_summary(
                    str(data.get("run_id", "")),
                    str(data.get("artifact_path", "")),
                )
            elif action_value == "complete_direct_proof":
                result = self.complete_direct_proof(
                    str(data.get("run_id", "")),
                    str(data.get("artifact_path", "")),
                )
            else:  # status
                result = self.status(str(data.get("run_id", "")))

            run_id = self._result_run_id(result, data)
            if run_id and action_value != "status":
                self._record_event(action_value, run_id, data, result)
                self._save_run_snapshot(run_id)

            # Persist the full run above, but return the compact status view to
            # the model. Retrieved abstracts can make a full run 200k+ chars.
            tool_result = (
                self.status(result.id)["run"]
                if isinstance(result, ResearchRun)
                else self._jsonable(result)
            )
            return json.dumps(
                {"ok": True, "action": action_value, "result": tool_result},
                ensure_ascii=False,
                indent=2,
            )
        except (WorkflowError, ValueError, KeyError, FileNotFoundError) as exc:
            if str(action).strip().lower() == "literature":
                run = self.runs.get(str((payload or {}).get("run_id", "")))
                if run is not None and run.stage == ResearchStage.LITERATURE_SURVEY:
                    run.literature_status = "failed"
                    run.stage = ResearchStage.FAILED
                    self._literature_event(run, "literature.failed", {
                        "error_type": type(exc).__name__, "message": str(exc)[:200],
                    })
                    self._save_run_snapshot(run.id)
            return json.dumps(
                {
                    "ok": False,
                    "action": str(action),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                indent=2,
            )

    def _restore_persisted_state(self) -> None:
        loader = self.deps.load_snapshots
        if loader is None:
            return
        try:
            snapshots = list(loader() or [])
        except Exception as exc:
            self.deps.terminal_print(f"  [research trace warning] restore failed: {exc}")
            return
        restored = 0
        for snapshot in snapshots:
            try:
                run = ResearchRun.from_dict(dict(snapshot.get("run") or {}))
                if not run.id:
                    continue
                self.runs[run.id] = run
                for item in snapshot.get("bindings", []) or []:
                    binding = MethodExecutionBinding.from_dict(item)
                    if binding.run_id == run.id and binding.method_id:
                        self.bindings[(run.id, binding.method_id)] = binding
                restored += 1
            except Exception as exc:
                self.deps.terminal_print(
                    f"  [research trace warning] ignored invalid snapshot: {exc}"
                )
        if restored:
            self.deps.terminal_print(
                f"  [research] restored {restored} workflow run(s) from trace store"
            )

    def _save_run_snapshot(self, run_id: str) -> None:
        saver = self.deps.save_snapshot
        if saver is None:
            return
        run = self.runs.get(run_id)
        if run is None:
            return
        bindings = [
            binding.to_dict()
            for (binding_run_id, _), binding in self.bindings.items()
            if binding_run_id == run_id
        ]
        try:
            saver(run_id, run.to_dict(), bindings)
        except Exception as exc:
            self.deps.terminal_print(
                f"  [research trace warning] snapshot save failed for {run_id}: {exc}"
            )

    def _record_event(
        self,
        action: str,
        run_id: str,
        data: Mapping[str, Any],
        result: Any,
    ) -> None:
        recorder = self.deps.record_event
        if recorder is None:
            return
        payload: dict[str, Any] = {"action": action}
        for key in (
            "method_id", "difficulty", "verdict", "regulator_action",
            "selected_method_ids", "artifact_path", "auto_decompose",
        ):
            if key in data:
                payload[key] = data[key]
        if isinstance(result, ResearchRun):
            payload["research_stage"] = result.stage.value
            payload["methods"] = len(result.methods)
        elif isinstance(result, MethodAttempt):
            payload["method_stage"] = result.stage.value
            payload["structural_verdict"] = (
                result.structural_verdict.value if result.structural_verdict else None
            )
            payload["detailed_verdict"] = (
                result.detailed_verdict.value if result.detailed_verdict else None
            )
            payload["proof_revision_count"] = result.proof_revision_count
            payload["plan_revision_count"] = result.plan_revision_count
            payload["rewrite_count"] = result.rewrite_count
        elif isinstance(result, MethodExecutionBinding):
            payload["method_id"] = result.method_id
            payload["plan_generation"] = result.plan_generation
            payload["task_count"] = len(result.step_task_ids)
        try:
            recorder(action, run_id, payload)
        except Exception as exc:
            self.deps.terminal_print(
                f"  [research trace warning] event save failed for {run_id}: {exc}"
            )

    @staticmethod
    def _result_run_id(result: Any, data: Mapping[str, Any]) -> str:
        if isinstance(result, ResearchRun):
            return result.id
        if isinstance(result, MethodExecutionBinding):
            return result.run_id
        if isinstance(result, MethodAttempt):
            return str(data.get("run_id", ""))
        return str(data.get("run_id", ""))

    def status(self, run_id: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        run_data = run.to_dict()
        # The trace snapshot keeps complete evidence. Status is a compact tool
        # view: embedding every full source in every retrieval repeats abstracts
        # and can make one status response larger than the model context.
        run_data.pop("literature_sources", None)
        run_data["retrieved_source_count"] = len(run.retrieved_sources)
        run_data["retrieved_sources"] = [
            {
                "source_id": source.get("source_id"),
                "title": source.get("title"),
                "provider": source.get("provider"),
                "published_year": source.get("published_year"),
            }
            for source in run.retrieved_sources
        ]
        run_data["literature_retrievals"] = [
            {
                "query": item.get("query"),
                "provider": item.get("provider"),
                "status": item.get("status"),
                "result_count": item.get("result_count"),
                "raw_result_count": item.get("raw_result_count"),
                "deduplicated_result_count": item.get("deduplicated_result_count"),
                "latency_ms": item.get("latency_ms"),
                "provider_statuses": item.get("provider_statuses", {}),
            }
            for item in run.literature_retrievals
        ]
        return {
            "run": run_data,
            "bindings": [
                binding.to_dict()
                for (binding_run_id, _), binding in self.bindings.items()
                if binding_run_id == run.id
            ],
        }

    # --------------------------------------------------------------- internals
    def _sync_revision_task(
        self,
        run: ResearchRun,
        method: MethodAttempt,
        binding: MethodExecutionBinding,
    ) -> None:
        task_id = binding.revision_task_id
        if not task_id:
            raise WorkflowValidationError(
                f"method {method.id} has pending proof revision without a durable revision task"
            )
        task = self.deps.load_task(task_id)
        if str(task.status) in {"failed", "cancelled"}:
            self.workflow.fail_method(run, method.id)
            return
        if str(task.status) == "completed":
            self.workflow.complete_proof_revision(run, method.id)
            binding.revision_task_id = None
            self._spawn_structural_reviewer(run, method, binding)

    def _ensure_method_worktree(self, binding: MethodExecutionBinding) -> None:
        if self.deps.create_worktree is None or self.deps.bind_task_to_worktree is None:
            return
        task_ids = [binding.step_task_ids[step_id] for step_id in binding.step_order]
        if not task_ids:
            return
        if binding.worktree_name is None:
            base = f"rw_{self._safe_id(binding.run_id)}_{self._safe_id(binding.method_id)}"
            binding.worktree_name = base[:64]
            result = self.deps.create_worktree(binding.worktree_name, task_ids[0])
            lowered = str(result).lower()
            if lowered.startswith("error") or lowered.startswith("git error"):
                binding.worktree_name = None
                raise WorkflowError(f"failed to create method worktree: {result}")
            remaining = task_ids[1:]
        else:
            remaining = task_ids
        for task_id in remaining:
            self.deps.bind_task_to_worktree(task_id, binding.worktree_name)

    def _spawn_structural_reviewer(
        self,
        run: ResearchRun,
        method: MethodAttempt,
        binding: MethodExecutionBinding,
    ) -> None:
        name = f"structural_{self._safe_id(run.id)}_{method.id}_{method.proof_revision_count}"
        binding.structural_reviewer = name
        role = "Structural Mathematical Reviewer"
        prompt = (
            f"Audit method {method.id} ({method.name}) for research run {run.id}.\n"
            f"Read cumulative proof: {binding.review_path()}\n"
            "Check logical architecture, hypotheses, boundary conditions, functional spaces, "
            "dependency consistency, and whether every proof step actually establishes its goal. "
            "Check compactness claims in their stated source and target spaces.\n"
            "Return ONE JSON object to Lead with schema:\n"
            '{"verdict":"done|continue","issues":[{"type":"...","location":"...","description":"..."}]}\n'
            "Do not decide the next workflow stage yourself."
        )
        self._spawn_or_wake(name, role, prompt)

    def _spawn_detailed_reviewer(
        self,
        run: ResearchRun,
        method: MethodAttempt,
        binding: MethodExecutionBinding,
        structural_issues: Sequence[Mapping[str, Any]],
    ) -> None:
        name = f"detailed_{self._safe_id(run.id)}_{method.id}_{method.proof_revision_count}"
        binding.detailed_reviewer = name
        role = "Detailed Mathematical Verifier"
        prompt = (
            f"Perform a line-by-line detailed verification of {binding.review_path()} for "
            f"method {method.id} ({method.name}).\n"
            "Check coefficients, signs, estimates, domains, differentiations/integrations, "
            "normalizations and every claimed identity. Verify that each remainder bound follows "
            "from a stated uniform estimate, that symmetry agrees with the Taylor expansion, "
            "and that stability claims have a specified evolution equation. "
            "Use symbolic verification when useful.\n"
            f"Structural-review context: {json.dumps(list(structural_issues), ensure_ascii=False)}\n"
            "Return ONE JSON object to Lead with schema:\n"
            '{"verdict":"done|continue","issues":[{"type":"...","location":"...","description":"..."}]}\n'
            "Do not skip directly to summary."
        )
        self._spawn_or_wake(name, role, prompt)

    def _spawn_regulator(
        self,
        run: ResearchRun,
        method: MethodAttempt,
        binding: MethodExecutionBinding,
        source: str,
        issues: Sequence[Mapping[str, Any]],
    ) -> None:
        name = f"regulator_{self._safe_id(run.id)}_{method.id}_{method.proof_revision_count}_{method.plan_revision_count}"
        binding.regulator_name = name
        role = "Mathematical Research Regulator"
        prompt = (
            f"Regulate method {method.id} ({method.name}) after {source} verification returned CONTINUE.\n"
            f"Proof path: {binding.review_path()}\n"
            f"Issues: {json.dumps(list(issues), ensure_ascii=False)}\n"
            "Choose exactly one action based on the mathematical defect:\n"
            "- revise_proof: method/plan is sound; patch proof details only.\n"
            "- revise_plan: decomposition is incomplete or incorrectly ordered.\n"
            "- rewrite: current method attempt needs a substantially new decomposition.\n"
            "Return ONE JSON object to Lead with schema:\n"
            '{"action":"revise_proof|revise_plan|rewrite","notes":"precise instructions"}'
        )
        self._spawn_or_wake(name, role, prompt)

    def _spawn_or_wake(self, name: str, role: str, prompt: str) -> str:
        result = self.deps.spawn_teammate(name, role, prompt)
        if "already exists" in result.lower():
            return self.deps.send_message(name, prompt)
        return result

    @staticmethod
    def _decomposition_prompt(run: ResearchRun, method: MethodAttempt) -> str:
        return (
            f"Decompose candidate method {method.id}: {method.name}\n"
            f"Research problem reference: {run.problem_ref}\n"
            f"Rationale from literature survey: {method.rationale}\n"
            "Before any proof work, produce a dependency-aware list of atomic mathematical steps. "
            "Do not prove the steps yet. Return ONE JSON object to Lead with schema:\n"
            '{"method_id":"%s","steps":[{"id":"S1","goal":"...","depends_on":[]},'
            '{"id":"S2","goal":"...","depends_on":["S1"]}]}\n'
            "Use enough steps to expose hidden hypotheses and nontrivial transitions."
        ) % method.id

    @staticmethod
    def _step_task_description(
        run: ResearchRun,
        method: MethodAttempt,
        step_id: str,
        goal: str,
        binding: MethodExecutionBinding,
    ) -> str:
        return (
            f"Research run: {run.id}\n"
            f"Method: {method.id} — {method.name}\n"
            f"Workflow step: {step_id}\n"
            f"Goal: {goal}\n"
            f"Cumulative proof file: {binding.proof_path}\n"
            "Read the cumulative proof before writing. Prove only this durable step, append a "
            "rigorous derivation to the cumulative proof file, then complete this task."
        )

    @staticmethod
    def _prover_prompt(
        run: ResearchRun,
        method: MethodAttempt,
        binding: MethodExecutionBinding,
    ) -> str:
        task_lines = "\n".join(
            f"- {step_id}: task {binding.step_task_ids[step_id]}"
            for step_id in binding.step_order
        )
        return (
            f"You own candidate method {method.id}: {method.name} for research run {run.id}.\n"
            f"Cumulative proof file: {binding.proof_path}\n"
            "The decomposition has already been approved by the workflow. The durable TaskStore "
            "is authoritative; do not replace it with a private plan. Claim and complete only "
            "the following method tasks in dependency order:\n"
            f"{task_lines}\n"
            "For each task: read prior proof, prove exactly the requested step, append the rigorous "
            "derivation, and complete the durable task."
        )

    @staticmethod
    def _topological_step_ids(method: MethodAttempt) -> list[str]:
        graph = {step.id: tuple(step.depends_on) for step in method.steps}
        order: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

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
            order.append(step_id)

        for step in method.steps:
            visit(step.id)
        return order

    def _require_run(self, run_id: str) -> ResearchRun:
        run = self.runs.get(str(run_id))
        if run is None:
            raise WorkflowValidationError(f"unknown research run id: {run_id}")
        return run

    @staticmethod
    def _require_method(run: ResearchRun, method_id: str) -> MethodAttempt:
        method = run.get_method(str(method_id))
        if method is None:
            raise WorkflowValidationError(f"unknown method id: {method_id}")
        return method

    def _require_binding(self, run_id: str, method_id: str) -> MethodExecutionBinding:
        binding = self.bindings.get((str(run_id), str(method_id)))
        if binding is None:
            raise WorkflowValidationError(
                f"method {method_id} has no execution binding; activate its decomposition first"
            )
        return binding

    @staticmethod
    def _safe_id(value: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in str(value))[:48]

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if isinstance(value, ResearchRun):
            return value.to_dict()
        if isinstance(value, MethodAttempt):
            return value.to_dict()
        if isinstance(value, MethodExecutionBinding):
            return value.to_dict()
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            return value
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return value
