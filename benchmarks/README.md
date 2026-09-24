# Mathematical Research Benchmark

This is a small acceptance benchmark for the existing research workflow. It runs four self-contained mathematical problems through the real Python workflow coordinator and orchestrator, SQLite TaskStore dependency graph, TraceStore snapshots/events, and ResearchEvaluation summary.

## Run

From the repository root:

```powershell
python benchmarks/run_benchmark.py --mode offline
python benchmarks/run_benchmark.py --mode offline --case case_04
python -m unittest tests.test_benchmark -v
```

The default output is JSON on stdout. Each case also writes `result.json`, `tasks.db`, `traces.db`, `problem.md`, and `proof.md` below `benchmarks/results/`. Use `--output-dir PATH` to choose another location. Generated results are ignored by Git. A nonzero exit code means a case failed its checklist or the runner could not execute.

To inspect one run's workflow trace:

```powershell
python -m agent_runtime.observability.viewer --db "benchmarks/results/<run-directory>/traces.db" --research-run benchmark_case_04
```

The exact run directory appears in `result_dir` in the output.

## Cases and expected behavior

| Case | Mathematical task | Offline workflow checks |
| --- | --- | --- |
| `case_01` | Elliptic maximum principle | Literature classification, easy direct-proof branch, final artifact |
| `case_02` | Neumann principal eigenvalue | Decomposition, four dependent proof steps, both verifier gates |
| `case_03` | Reaction–diffusion linear stability | Two candidate methods, TaskStore failure injection for one method, Summary selects only the completed method |
| `case_04` | Dirichlet simple-eigenvalue bifurcation | Six dependent steps; Structural CONTINUE → Regulator REVISE_PROOF → durable revision task → Structural DONE → Detailed DONE |

Case specifications live in `cases/`; thresholds live in `expected/`. Checks use workflow state, event ordering, durable task dependencies, verifier counts, snapshot restoration, selected method IDs, and artifact existence. They do **not** compare a final proof to an exact answer string.

## What offline mode means

Offline mode uses deterministic fixture decisions for literature classification, decomposition, verifier verdicts, and regulator action. The teammate callbacks are local fakes; no Anthropic client, MCP server, network request, or API key is needed. The runner executes real workflow transitions and SQLite task claims/completions. It restarts the orchestrator from TraceStore snapshots and checks that this does not create duplicate tasks or teammates. For `case_03` only, the runner marks one claimed task failed directly in its private fixture database because TaskStore has no public failure method; the orchestrator then observes that failed status through its normal `sync_method` path.

The generated `proof.md` is explicitly marked **NOT A MATHEMATICAL PROOF**. Scripted DONE verdicts certify that the state-machine route was exercised, not that the mathematics is correct. Likewise, the literature notes are fixtures, not a live survey.

Offline metrics for model calls, tool calls, tokens, and cost are `null` because no model/runtime tool usage is measured. `latency_ms` is runner wall-clock duration, including local SQLite operations. Revision thresholds in `expected/` describe the deterministic offline scenario; a future live run should not be required to make an artificial mistake in `case_04`.

## Live mode boundary

`--mode live` currently reports a clear error. Without `ANTHROPIC_API_KEY` it says `live mode requires Anthropic credentials`. With credentials, it explains that the project currently exposes an interactive Lead CLI but no non-interactive benchmark entry point. Use `python s20.py` for a manual live smoke test. This benchmark does not label scripted responses as live agent output.

## Scope and limitations

This benchmark tests routing and persistence at the workflow boundary. It does not grade mathematical proof correctness, simulate a real crash between separate SQLite commits, recreate lost teammate threads, measure live model usage, or certify OS isolation. The fixture cases are intentionally small and keep normal unit tests independent of Anthropic access.

