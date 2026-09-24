from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Difficulty(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class ResearchStage(str, Enum):
    LITERATURE_SURVEY = "literature_survey"
    DIRECT_PROOF = "direct_proof"
    METHODS_ACTIVE = "methods_active"
    SUMMARY = "summary"
    COMPLETED = "completed"
    FAILED = "failed"


class MethodStage(str, Enum):
    DECOMPOSITION = "decomposition"
    PROVING = "proving"
    STRUCTURAL_VERIFY = "structural_verify"
    DETAILED_VERIFY = "detailed_verify"
    REGULATION = "regulation"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class ProofStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class Verdict(str, Enum):
    DONE = "done"
    CONTINUE = "continue"


class RegulatorAction(str, Enum):
    REVISE_PROOF = "revise_proof"
    REVISE_PLAN = "revise_plan"
    REWRITE = "rewrite"


@dataclass
class ProofStep:
    id: str
    goal: str
    depends_on: tuple[str, ...] = ()
    status: ProofStepStatus = ProofStepStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "depends_on": list(self.depends_on),
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProofStep":
        return cls(
            id=str(data.get("id", "")),
            goal=str(data.get("goal", "")),
            depends_on=tuple(str(item) for item in data.get("depends_on", ()) or ()),
            status=ProofStepStatus(str(data.get("status", "pending"))),
        )


@dataclass
class MethodAttempt:
    id: str
    name: str
    rationale: str = ""
    source_ids: tuple[str, ...] = ()
    origin: str = "model_reasoning"
    teammate: str | None = None
    stage: MethodStage = MethodStage.DECOMPOSITION
    steps: list[ProofStep] = field(default_factory=list)
    structural_verdict: Verdict | None = None
    detailed_verdict: Verdict | None = None
    regulator_action: RegulatorAction | None = None
    regulator_notes: str = ""
    revision_pending: bool = False
    proof_revision_count: int = 0
    plan_revision_count: int = 0
    rewrite_count: int = 0

    def get_step(self, step_id: str) -> ProofStep | None:
        return next((step for step in self.steps if step.id == step_id), None)

    def all_steps_completed(self) -> bool:
        return bool(self.steps) and all(
            step.status == ProofStepStatus.COMPLETED for step in self.steps
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "rationale": self.rationale,
            "source_ids": list(self.source_ids),
            "origin": self.origin,
            "teammate": self.teammate,
            "stage": self.stage.value,
            "steps": [step.to_dict() for step in self.steps],
            "structural_verdict": (
                self.structural_verdict.value if self.structural_verdict else None
            ),
            "detailed_verdict": (
                self.detailed_verdict.value if self.detailed_verdict else None
            ),
            "regulator_action": (
                self.regulator_action.value if self.regulator_action else None
            ),
            "regulator_notes": self.regulator_notes,
            "revision_pending": self.revision_pending,
            "proof_revision_count": self.proof_revision_count,
            "plan_revision_count": self.plan_revision_count,
            "rewrite_count": self.rewrite_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MethodAttempt":
        structural = data.get("structural_verdict")
        detailed = data.get("detailed_verdict")
        regulator = data.get("regulator_action")
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            rationale=str(data.get("rationale", "")),
            source_ids=tuple(str(x) for x in data.get("source_ids", []) or []),
            origin=str(data.get("origin", "model_reasoning")),
            teammate=(str(data["teammate"]) if data.get("teammate") is not None else None),
            stage=MethodStage(str(data.get("stage", "decomposition"))),
            steps=[ProofStep.from_dict(item) for item in data.get("steps", []) or []],
            structural_verdict=(Verdict(str(structural)) if structural else None),
            detailed_verdict=(Verdict(str(detailed)) if detailed else None),
            regulator_action=(RegulatorAction(str(regulator)) if regulator else None),
            regulator_notes=str(data.get("regulator_notes", "")),
            revision_pending=bool(data.get("revision_pending", False)),
            proof_revision_count=int(data.get("proof_revision_count") or 0),
            plan_revision_count=int(data.get("plan_revision_count") or 0),
            rewrite_count=int(data.get("rewrite_count") or 0),
        )


@dataclass
class ResearchRun:
    id: str
    problem_ref: str
    stage: ResearchStage = ResearchStage.LITERATURE_SURVEY
    difficulty: Difficulty | None = None
    literature_notes: str = ""
    literature_status: str = "pending"
    literature_sources: list[dict[str, Any]] = field(default_factory=list)
    retrieved_sources: list[dict[str, Any]] = field(default_factory=list)
    selected_sources: list[dict[str, str]] = field(default_factory=list)
    literature_retrievals: list[dict[str, Any]] = field(default_factory=list)
    methods: list[MethodAttempt] = field(default_factory=list)
    selected_method_ids: tuple[str, ...] = ()
    final_artifact: str | None = None

    def get_method(self, method_id: str) -> MethodAttempt | None:
        return next((method for method in self.methods if method.id == method_id), None)

    def completed_methods(self) -> list[MethodAttempt]:
        return [
            method for method in self.methods if method.stage == MethodStage.COMPLETED
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "problem_ref": self.problem_ref,
            "stage": self.stage.value,
            "difficulty": self.difficulty.value if self.difficulty else None,
            "literature_notes": self.literature_notes,
            "literature_status": self.literature_status,
            "literature_sources": list(self.literature_sources),
            "retrieved_sources": list(self.retrieved_sources),
            "selected_sources": list(self.selected_sources),
            "literature_retrievals": list(self.literature_retrievals),
            "methods": [method.to_dict() for method in self.methods],
            "selected_method_ids": list(self.selected_method_ids),
            "final_artifact": self.final_artifact,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchRun":
        difficulty = data.get("difficulty")
        return cls(
            id=str(data.get("id", "")),
            problem_ref=str(data.get("problem_ref", "")),
            stage=ResearchStage(str(data.get("stage", "literature_survey"))),
            difficulty=(Difficulty(str(difficulty)) if difficulty else None),
            literature_notes=str(data.get("literature_notes", "")),
            literature_status=str(data.get("literature_status", "pending")),
            literature_sources=[dict(item) for item in data.get("literature_sources", []) or []],
            retrieved_sources=[dict(item) for item in data.get("retrieved_sources", data.get("literature_sources", [])) or []],
            selected_sources=[dict(item) for item in data.get("selected_sources", []) or []],
            literature_retrievals=[dict(item) for item in data.get("literature_retrievals", []) or []],
            methods=[MethodAttempt.from_dict(item) for item in data.get("methods", []) or []],
            selected_method_ids=tuple(str(item) for item in data.get("selected_method_ids", ()) or ()),
            final_artifact=(str(data["final_artifact"]) if data.get("final_artifact") is not None else None),
        )
