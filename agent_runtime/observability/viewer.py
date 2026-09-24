from __future__ import annotations

import argparse
import json
from pathlib import Path

from .evaluation import evaluate_research_run
from .store import TraceStore


class TraceViewer:
    """Small terminal viewer for runtime and mathematical-workflow traces."""

    def __init__(self, store: TraceStore):
        self.store = store

    def render_runtime_run(self, run_id: str) -> str:
        s = self.store.runtime_run_summary(run_id)
        cost = (
            "unconfigured"
            if s["estimated_cost_usd"] is None
            else f"${float(s['estimated_cost_usd']):.6f}"
        )
        return "\n".join(
            [
                f"Runtime Run: {s['run_id']}",
                f"Agent: {s['agent_name'] or '?'} ({s['agent_kind'] or '?'})",
                f"Role: {s['role'] or '-'}",
                f"Status: {s['status']}",
                f"Started: {s['started_at']}",
                f"Ended: {s['ended_at'] or '-'}",
                f"Model calls: {s['model_calls']}  latency={s['model_latency_ms']:.1f}ms  retries={s['retry_count']}",
                f"Tokens: input={s['input_tokens']} output={s['output_tokens']} cache_read={s['cache_read_input_tokens']} cache_write={s['cache_creation_input_tokens']}",
                f"Estimated cost: {cost}",
                f"Tool calls: {s['tool_calls']}  blocked={s['blocked_tools']}",
                f"Permissions: total={s['permission_decisions']} ask={s['permission_asked']} denied={s['permission_denied']}",
            ]
        )

    def render_research_run(self, research_run_id: str) -> str:
        snapshot = self.store.research_snapshot(research_run_id)
        ev = evaluate_research_run(self.store, research_run_id)
        lines = [
            f"Research Run: {research_run_id}",
            f"Stage: {ev.stage}  Difficulty: {ev.difficulty or '-'}",
            f"Methods: total={ev.methods_total} completed={ev.methods_completed} failed={ev.methods_failed} abandoned={ev.methods_abandoned}",
            f"Proof steps: {ev.proof_steps_completed}/{ev.proof_steps_total} completed",
            f"Revisions: proof={ev.proof_revisions} plan={ev.plan_revisions} rewrites={ev.rewrites}",
            f"Final artifact: {ev.final_artifact or '-'}",
            "Methods:",
        ]
        for method in snapshot["run"].get("methods", []):
            lines.append(
                f"  - {method.get('id')}: {method.get('name')} [{method.get('stage')}] "
                f"structural={method.get('structural_verdict') or '-'} "
                f"detailed={method.get('detailed_verdict') or '-'}"
            )
        return "\n".join(lines)

    def render_recent(self, limit: int = 10) -> str:
        rows = self.store.list_recent_runtime_runs(limit)
        if not rows:
            return "(no runtime traces)"
        return "\n".join(
            f"{row['run_id']}  {row['agent_name'] or '?'}  {row['status']}  "
            f"llm={row['model_calls']} tools={row['tool_calls']} tokens={row['input_tokens'] + row['output_tokens']}"
            for row in rows
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="ResearchAgent trace viewer")
    parser.add_argument("--db", default=".state/traces.db")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--run")
    group.add_argument("--research-run")
    group.add_argument("--recent", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    store = TraceStore(Path(args.db))
    viewer = TraceViewer(store)
    if args.run:
        value = store.runtime_run_summary(args.run) if args.json else viewer.render_runtime_run(args.run)
    elif args.research_run:
        value = (
            evaluate_research_run(store, args.research_run).to_dict()
            if args.json
            else viewer.render_research_run(args.research_run)
        )
    else:
        value = store.list_recent_runtime_runs(args.recent) if args.json else viewer.render_recent(args.recent)
    print(json.dumps(value, ensure_ascii=False, indent=2) if args.json else value)


if __name__ == "__main__":
    main()
