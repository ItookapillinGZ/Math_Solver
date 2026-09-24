from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from .store import TraceStore


@dataclass(frozen=True)
class ResearchEvaluation:
    research_run_id: str
    stage: str
    difficulty: str | None
    methods_total: int
    methods_completed: int
    methods_failed: int
    methods_abandoned: int
    proof_steps_total: int
    proof_steps_completed: int
    structural_done: int
    structural_continue: int
    detailed_done: int
    detailed_continue: int
    proof_revisions: int
    plan_revisions: int
    rewrites: int
    final_artifact: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_research_run(store: TraceStore, research_run_id: str) -> ResearchEvaluation:
    snapshot = store.research_snapshot(research_run_id)
    run = snapshot["run"]
    methods = list(run.get("methods") or [])
    steps = [step for method in methods for step in (method.get("steps") or [])]
    events = store.list_workflow_events(research_run_id)

    def verdict_count(event_type: str, verdict: str, field: str) -> int:
        recorded = [event for event in events if event["event_type"] == event_type]
        if recorded:
            return sum(
                event["payload"].get("verdict") == verdict for event in recorded
            )
        # Older or externally imported snapshots may have no verdict events.
        return sum(method.get(field) == verdict for method in methods)

    return ResearchEvaluation(
        research_run_id=research_run_id,
        stage=str(run.get("stage") or ""),
        difficulty=run.get("difficulty"),
        methods_total=len(methods),
        methods_completed=sum(m.get("stage") == "completed" for m in methods),
        methods_failed=sum(m.get("stage") == "failed" for m in methods),
        methods_abandoned=sum(m.get("stage") == "abandoned" for m in methods),
        proof_steps_total=len(steps),
        proof_steps_completed=sum(s.get("status") == "completed" for s in steps),
        structural_done=verdict_count("structural_verdict", "done", "structural_verdict"),
        structural_continue=verdict_count("structural_verdict", "continue", "structural_verdict"),
        detailed_done=verdict_count("detailed_verdict", "done", "detailed_verdict"),
        detailed_continue=verdict_count("detailed_verdict", "continue", "detailed_verdict"),
        proof_revisions=sum(int(m.get("proof_revision_count") or 0) for m in methods),
        plan_revisions=sum(int(m.get("plan_revision_count") or 0) for m in methods),
        rewrites=sum(int(m.get("rewrite_count") or 0) for m in methods),
        final_artifact=run.get("final_artifact"),
    )
