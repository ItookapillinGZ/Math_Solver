#!/usr/bin/env python3
"""Small deterministic acceptance benchmark for the real research workflow gates.

Offline decisions are fixtures, not mathematical proofs or model outputs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.observability.evaluation import evaluate_research_run
from agent_runtime.observability.store import TraceStore
from agent_runtime.research.orchestration import (
    ResearchWorkflowOrchestrator,
    ResearchWorkflowOrchestratorDependencies,
)
from agent_runtime.research.workflow import ResearchWorkflowCoordinator
from agent_runtime.tasks.runtime import TaskRuntime, TaskRuntimeConfig, TaskRuntimeDependencies
from agent_runtime.tasks.store import TaskStore

BENCHMARK_DIR = Path(__file__).resolve().parent
CASE_DIR = BENCHMARK_DIR / "cases"
EXPECTED_DIR = BENCHMARK_DIR / "expected"
EXPECTED_COUNTS = (
    "min_methods", "min_steps", "min_completed_steps", "min_failed_methods_offline",
    "min_structural_done", "min_detailed_done", "min_proof_revisions_offline",
)


def load_case(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: case must be an object")
    case_id = data.get("id")
    if not isinstance(case_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", case_id):
        raise ValueError(f"{path}: invalid case id")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError(f"{path}: title is required")
    if not isinstance(data.get("problem"), str) or not data["problem"].strip():
        raise ValueError(f"{path}: problem is required")
    if data.get("difficulty") not in {"easy", "medium", "hard"}:
        raise ValueError(f"{path}: difficulty must be easy, medium, or hard")
    scenario = data.get("offline_scenario")
    if not isinstance(scenario, dict) or not isinstance(scenario.get("methods"), list):
        raise ValueError(f"{path}: offline_scenario.methods must be a list")
    methods = scenario["methods"]
    if (data["difficulty"] == "easy") != (len(methods) == 0):
        raise ValueError(f"{path}: easy requires zero methods; medium/hard require methods")
    for method in methods:
        if not isinstance(method, dict) or not all(
            isinstance(method.get(key), str) and method[key].strip()
            for key in ("name", "rationale")
        ):
            raise ValueError(f"{path}: method needs name and rationale")
        if method.get("outcome") not in {"complete", "fail_first_task", "revise_proof"}:
            raise ValueError(f"{path}: invalid offline method outcome")
        steps = method.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"{path}: method needs proof steps")
        ids = [item.get("id") for item in steps if isinstance(item, dict)]
        if len(ids) != len(steps) or len(set(ids)) != len(ids) or not all(
            isinstance(item, str) and item for item in ids
        ):
            raise ValueError(f"{path}: step ids must be unique nonempty strings")
        for item in steps:
            if not isinstance(item.get("goal"), str) or not item["goal"].strip():
                raise ValueError(f"{path}: step goal is required")
            deps = item.get("depends_on")
            if not isinstance(deps, list) or any(dep not in ids or dep == item["id"] for dep in deps):
                raise ValueError(f"{path}: invalid step dependencies")
    return data


def load_expected(path: Path, case_id: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("case_id") != case_id:
        raise ValueError(f"{path}: expected case_id mismatch")
    if data.get("branch") not in {"direct_proof", "methods"}:
        raise ValueError(f"{path}: invalid expected branch")
    for key in EXPECTED_COUNTS:
        if type(data.get(key)) is not int or data[key] < 0:
            raise ValueError(f"{path}: {key} must be a nonnegative integer")
    if type(data.get("require_final_artifact")) is not bool:
        raise ValueError(f"{path}: require_final_artifact must be boolean")
    return data


def load_cases(case_id: str | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    paths = sorted(CASE_DIR.glob("*.json"))
    if case_id:
        paths = [path for path in paths if path.name.startswith(case_id + "_")]
    if not paths:
        raise ValueError(f"no benchmark cases found for {case_id or 'all'}")
    pairs = []
    seen = set()
    for path in paths:
        case = load_case(path)
        if case["id"] in seen:
            raise ValueError(f"duplicate case id {case['id']}")
        seen.add(case["id"])
        expected = load_expected(EXPECTED_DIR / f"{case['id']}.json", case["id"])
        if (case["difficulty"] == "easy") != (expected["branch"] == "direct_proof"):
            raise ValueError(f"{case['id']}: branch does not match difficulty")
        pairs.append((case, expected))
    return pairs


def _dispatch(orchestrator: ResearchWorkflowOrchestrator, action: str, **payload: Any) -> Any:
    response = json.loads(orchestrator.dispatch(action, payload))
    if not response["ok"]:
        raise RuntimeError(f"{action}: {response['error']}")
    return response["result"]


def _rejected(orchestrator: ResearchWorkflowOrchestrator, action: str, **payload: Any) -> bool:
    return not json.loads(orchestrator.dispatch(action, payload))["ok"]


def _make_orchestrator(
    case_id: str, runtime: TaskRuntime, trace: TraceStore, spawned: list[dict[str, str]]
) -> ResearchWorkflowOrchestrator:
    def spawn(name: str, role: str, prompt: str) -> str:
        spawned.append({"name": name, "role": role})
        return f"offline fixture teammate {name}"

    def send(to: str, content: str) -> str:
        return f"offline fixture message to {to}"

    return ResearchWorkflowOrchestrator(
        ResearchWorkflowCoordinator(run_id_factory=lambda: f"benchmark_{case_id}"),
        ResearchWorkflowOrchestratorDependencies(
            create_task=runtime.create_task,
            load_task=runtime.load_task,
            spawn_teammate=spawn,
            send_message=send,
            terminal_print=lambda message: None,
            record_event=trace.record_workflow_event,
            save_snapshot=trace.save_workflow_snapshot,
            load_snapshots=trace.load_workflow_snapshots,
            allow_offline_literature_fixture=True,
        ),
    )


def _claim_and_complete(store: TaskStore, task_id: str) -> None:
    claim = store.claim_task(task_id, owner="offline-benchmark")
    if not claim.claimed:
        raise RuntimeError(f"task {task_id} could not be claimed: {claim.reason}")
    store.complete_task(task_id, owner="offline-benchmark")


def _inject_failed_task(store: TaskStore, task_id: str) -> None:
    """Fixture-only fault injection; TaskStore has no public fail_task API."""
    claim = store.claim_task(task_id, owner="offline-benchmark")
    if not claim.claimed:
        raise RuntimeError(f"fault injection could not claim {task_id}: {claim.reason}")
    with closing(sqlite3.connect(store.db_path)) as conn:
        with conn:
            cursor = conn.execute(
                "UPDATE tasks SET status='failed', lease_until=NULL WHERE id=? AND status='in_progress'",
                (task_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"fault injection did not fail {task_id}")


def evaluate_checklist(
    metrics: dict[str, Any],
    snapshot: dict[str, Any],
    expected: dict[str, Any],
    *,
    event_types: list[str],
    spawned_roles: list[str],
    dag_valid: bool,
    premature_summary_rejected: bool,
    detailed_gate_rejected: bool,
) -> dict[str, bool]:
    run = snapshot["run"]
    methods = list(run.get("methods") or [])
    selected = list(run.get("selected_method_ids") or [])
    artifact = metrics.get("final_artifact")
    checks = {
        "workflow_completed": metrics["final_stage"] == "completed",
        "difficulty_classified": metrics["difficulty"] in {"easy", "medium", "hard"},
        "candidate_methods": metrics["methods_total"] >= expected["min_methods"],
        "proof_steps": metrics["proof_steps_total"] >= expected["min_steps"],
        "proof_steps_completed": metrics["proof_steps_completed"] >= expected["min_completed_steps"],
        "failed_method_handled": metrics["methods_failed"] >= expected["min_failed_methods_offline"],
        "structural_verification": metrics["structural_done"] >= expected["min_structural_done"],
        "detailed_verification": metrics["detailed_done"] >= expected["min_detailed_done"],
        "proof_revision": metrics["proof_revisions"] >= expected["min_proof_revisions_offline"],
        "premature_summary_blocked": premature_summary_rejected,
        "summary_only_completed_methods": bool(selected) and all(
            any(method["id"] == method_id and method["stage"] == "completed" for method in methods)
            for method_id in selected
        ) if expected["branch"] == "methods" else not selected,
        "final_artifact_exists": bool(artifact and Path(artifact).is_file())
        if expected["require_final_artifact"] else True,
    }
    if expected["branch"] == "methods":
        checks.update({
            "decomposition_before_proving": (
                "literature" in event_types
                and "activate_method" in event_types
                and event_types.index("literature") < event_types.index("activate_method")
                and sum("Decomposer" in role for role in spawned_roles) >= len(methods)
            ),
            "taskstore_dependency_dag": dag_valid,
            "structural_before_detailed": (
                "structural_verdict" in event_types
                and "detailed_verdict" in event_types
                and event_types.index("structural_verdict") < event_types.index("detailed_verdict")
                and detailed_gate_rejected
            ),
        })
        if expected["min_proof_revisions_offline"]:
            sequence = ["structural_verdict", "regulator", "sync_method", "structural_verdict", "detailed_verdict"]
            iterator = iter(event_types)
            checks["regulator_revision_loop"] = all(any(item == wanted for item in iterator) for wanted in sequence)
    else:
        checks["direct_proof_branch"] = "complete_direct_proof" in event_types and not methods
    return checks


def run_offline_case(
    case: dict[str, Any], expected: dict[str, Any], output_dir: Path
) -> dict[str, Any]:
    started = time.perf_counter()
    case_dir = Path(output_dir).resolve() / (
        f"{case['id']}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    )
    case_dir.mkdir(parents=True)
    problem_path = case_dir / "problem.md"
    problem_path.write_text(f"# {case['title']}\n\n{case['problem']}\n", encoding="utf-8")
    proof_path = case_dir / "proof.md"
    store = TaskStore(case_dir / "tasks.db")
    trace = TraceStore(case_dir / "traces.db")
    next_task = iter(range(1, 10000))
    runtime = TaskRuntime(
        TaskRuntimeConfig(legacy_tasks_dir=case_dir / "legacy_tasks"),
        TaskRuntimeDependencies(
            store=store,
            terminal_print=lambda message: None,
            id_factory=lambda: f"task_{next(next_task):04d}",
        ),
    )
    spawned: list[dict[str, str]] = []
    orchestrator = _make_orchestrator(case["id"], runtime, trace, spawned)
    run_id = _dispatch(orchestrator, "start", problem_ref=str(problem_path))["id"]
    scenario = case["offline_scenario"]
    proposals = [
        {"name": method["name"], "rationale": method["rationale"]}
        for method in scenario["methods"]
    ]
    _dispatch(
        orchestrator, "literature", run_id=run_id, difficulty=case["difficulty"],
        literature_notes=scenario.get("literature_notes", ""), methods=proposals,
        offline_fixture=True,
    )
    premature_summary_rejected = _rejected(
        orchestrator, "begin_summary", run_id=run_id,
        selected_method_ids=["method_01"],
    )
    # A fresh instance must restore the snapshot without creating new tasks or teammates.
    task_count = len(store.list_tasks())
    spawn_count = len(spawned)
    orchestrator = _make_orchestrator(case["id"], runtime, trace, spawned)
    restored = run_id in orchestrator.runs and len(store.list_tasks()) == task_count and len(spawned) == spawn_count
    if not restored:
        raise RuntimeError("workflow snapshot did not restore without replay")

    dag_valid = True
    detailed_gate_rejected = case["difficulty"] == "easy"
    if case["difficulty"] == "easy":
        proof_path.write_text(
            "# OFFLINE WORKFLOW FIXTURE — NOT A MATHEMATICAL PROOF\n\n"
            "The direct-proof branch completed in the state machine. No LLM proof was generated.\n",
            encoding="utf-8",
        )
        _dispatch(orchestrator, "complete_direct_proof", run_id=run_id, artifact_path=str(proof_path))
    else:
        method_ids = [f"method_{index:02d}" for index in range(1, len(proposals) + 1)]
        bindings = {}
        for method_id, method in zip(method_ids, scenario["methods"]):
            binding = _dispatch(
                orchestrator, "activate_method", run_id=run_id, method_id=method_id,
                steps=method["steps"],
            )
            bindings[method_id] = binding
            task_ids = binding["step_task_ids"]
            for step in method["steps"]:
                actual = store.get_task(task_ids[step["id"]])
                expected_deps = {task_ids[dep] for dep in step["depends_on"]}
                dag_valid &= actual is not None and set(actual.blocked_by) == expected_deps
            dependent = next((step for step in method["steps"] if step["depends_on"]), None)
            if dependent:
                early_claim = store.claim_task(task_ids[dependent["id"]], owner="offline-benchmark")
                dag_valid &= not early_claim.claimed

        # Restore again after task materialization to exercise persisted bindings.
        task_count = len(store.list_tasks())
        spawn_count = len(spawned)
        orchestrator = _make_orchestrator(case["id"], runtime, trace, spawned)
        restored &= (
            run_id in orchestrator.runs
            and len(orchestrator.bindings) == len(bindings)
            and len(store.list_tasks()) == task_count
            and len(spawned) == spawn_count
        )
        completed_method_ids = []
        for method_id, method in zip(method_ids, scenario["methods"]):
            binding = bindings[method_id]
            if method["outcome"] == "fail_first_task":
                _inject_failed_task(store, binding["step_task_ids"][binding["step_order"][0]])
                synced = _dispatch(orchestrator, "sync_method", run_id=run_id, method_id=method_id)
                if synced["stage"] != "failed":
                    raise RuntimeError(f"{method_id}: injected task failure did not fail method")
                continue
            for step_id in binding["step_order"]:
                task_id = binding["step_task_ids"][step_id]
                if not store.can_start(task_id):
                    dag_valid = False
                _claim_and_complete(store, task_id)
                _dispatch(orchestrator, "sync_method", run_id=run_id, method_id=method_id)
            if method["outcome"] == "revise_proof":
                _dispatch(
                    orchestrator, "structural_verdict", run_id=run_id, method_id=method_id,
                    verdict="continue",
                    issues=[{"type": "gap", "location": "range/transversality", "description": "Fixture requests explicit range and transversality justification."}],
                )
                premature_summary_rejected &= _rejected(
                    orchestrator, "begin_summary", run_id=run_id,
                    selected_method_ids=[method_id],
                )
                _dispatch(
                    orchestrator, "regulator", run_id=run_id, method_id=method_id,
                    regulator_action="revise_proof",
                    notes="Supply the missing range and transversality justification.",
                )
                revision_id = orchestrator.bindings[(run_id, method_id)].revision_task_id
                if not revision_id:
                    raise RuntimeError("regulator did not create durable revision task")
                _claim_and_complete(store, revision_id)
                _dispatch(orchestrator, "sync_method", run_id=run_id, method_id=method_id)
            _dispatch(
                orchestrator, "structural_verdict", run_id=run_id,
                method_id=method_id, verdict="done",
            )
            detailed_gate_rejected |= _rejected(
                orchestrator, "begin_summary", run_id=run_id,
                selected_method_ids=[method_id],
            )
            _dispatch(
                orchestrator, "detailed_verdict", run_id=run_id,
                method_id=method_id, verdict="done",
            )
            completed_method_ids.append(method_id)
        proof_path.write_text(
            "# OFFLINE WORKFLOW FIXTURE — NOT A MATHEMATICAL PROOF\n\n"
            f"Case: {case['title']}\n\n"
            "All listed verifier decisions are scripted fixture values. "
            "No mathematical proof or literature survey was generated.\n",
            encoding="utf-8",
        )
        _dispatch(
            orchestrator, "begin_summary", run_id=run_id,
            selected_method_ids=completed_method_ids,
        )
        _dispatch(
            orchestrator, "complete_summary", run_id=run_id,
            artifact_path=str(proof_path),
        )

    evaluation = evaluate_research_run(trace, run_id).to_dict()
    snapshot = trace.research_snapshot(run_id)
    events = trace.list_workflow_events(run_id)
    event_types = [event["event_type"] for event in events]
    metrics = {
        "case_id": case["id"], "mode": "offline",
        "workflow_completed": evaluation["stage"] == "completed",
        "final_stage": evaluation["stage"], "difficulty": evaluation["difficulty"],
        "methods_total": evaluation["methods_total"],
        "methods_completed": evaluation["methods_completed"],
        "methods_failed": evaluation["methods_failed"],
        "methods_abandoned": evaluation["methods_abandoned"],
        "proof_steps_total": evaluation["proof_steps_total"],
        "proof_steps_completed": evaluation["proof_steps_completed"],
        "structural_done": evaluation["structural_done"],
        "structural_continue": evaluation["structural_continue"],
        "detailed_done": evaluation["detailed_done"],
        "detailed_continue": evaluation["detailed_continue"],
        "proof_revisions": evaluation["proof_revisions"],
        "plan_revisions": evaluation["plan_revisions"],
        "rewrites": evaluation["rewrites"],
        # No model or runtime tool runs occur offline, so these are unmeasured.
        "model_calls": None, "tool_calls": None,
        "input_tokens": None, "output_tokens": None, "cache_tokens": None,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "estimated_cost_usd": None,
        "final_artifact": evaluation["final_artifact"],
        "trace_db": str(trace.db_path),
        "task_db": str(store.db_path),
        "result_dir": str(case_dir),
        "snapshot_restored_without_replay": restored,
    }
    checks = evaluate_checklist(
        metrics, snapshot, expected, event_types=event_types,
        spawned_roles=[item["role"] for item in spawned],
        dag_valid=dag_valid, premature_summary_rejected=premature_summary_rejected,
        detailed_gate_rejected=detailed_gate_rejected,
    )
    checks["snapshot_restored_without_replay"] = restored
    metrics["checks"] = checks
    metrics["success"] = all(checks.values())
    (case_dir / "result.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--case", help="case id, for example case_04")
    parser.add_argument("--output-dir", type=Path, default=BENCHMARK_DIR / "results")
    args = parser.parse_args(argv)
    if args.mode == "live":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            parser.error("live mode requires Anthropic credentials")
        parser.error(
            "live mode is not wired to a non-interactive Lead entry point; "
            "use python s20.py for an interactive live smoke test"
        )
    try:
        pairs = load_cases(args.case)
        results = [run_offline_case(case, expected, args.output_dir) for case, expected in pairs]
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(2, f"benchmark error: {exc}\n")
    print(json.dumps(results, ensure_ascii=True, indent=2))
    return 0 if all(result["success"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

