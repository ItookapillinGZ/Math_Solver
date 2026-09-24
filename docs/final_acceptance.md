# Final Acceptance Report

Acceptance date: 2026-09-23.
Scope: final repository checks for the current ResearchAgent Runtime engineering cycle on Windows.
This report uses the final Phase G test and offline benchmark runs, not an earlier phase count.

## 1. Executive Summary

ResearchAgent Runtime is a local multi-agent orchestration runtime for mathematical research.
LLM agents choose methods, proof steps, and reviewer judgments; Python workflow rules enforce stage order.
SQLite stores durable tasks, jobs, messages, memory, workflow snapshots, and traces.
The current repository passed its full automated test suite and four-case offline workflow benchmark.
Its code, architecture documentation, benchmark, and interview guide are suitable for an engineering portfolio walkthrough.
This acceptance does not certify a production deployment, external MCP service availability, or formal mathematical correctness.

## 2. Final Architecture

The `s20.py` composition root wires the Anthropic gateway, Lead application, tool registry, policy, runtime services, research workflow, stores, and interactive CLI.
`ResearchWorkflowCoordinator` owns legal mathematical state transitions without I/O.
`ResearchWorkflowOrchestrator` maps those transitions to decomposer/prover/reviewer teammates, TaskStore proof-step DAGs, worktree hooks, workflow events, and recovery snapshots.
Tool policy and security executors constrain model-requested actions.
Configured MCP servers use persistent stdio or Streamable HTTP clients; TraceStore and ResearchEvaluation make outcomes inspectable.

Read the [README](../README.md), [architecture](architecture.md), [architecture audit](architecture_audit.md), and [resume/interview guide](resume_project.md) for details and limits.

## 3. Completed Engineering Areas

| Area | Current implementation |
| --- | --- |
| Runtime | Lead, ephemeral subagent, and autonomous teammate runtimes share execution records and an Anthropic model gateway. |
| Tool Registry | Catalog schemas and registered handlers form the tool-facing dispatch surface, including `research_workflow`. |
| Policy | Capability rules return ALLOW, DENY, or ASK before tool execution, with best-effort audit records. |
| Task durability | SQLite TaskStore stores dependencies, atomic claims, leases, heartbeats, recovery, and task-role affinity. |
| Jobs and scheduler | SQLite JobStore persists job/schedule state; background and cron paths have explicit retry behavior. |
| Context | Budgeting, compaction, checkpoints, and transcripts support longer sessions; token estimates are heuristic. |
| Memory | Scoped working, episodic, and artifact records persist in SQLite. |
| Team coordination | SQLite MessageStore backs teammate messages; the current bus consumes on read. |
| Safe execution | SymPy runs in a restricted child; commands use a no-shell allowlist; process limits and safe archive extraction add narrower controls. |
| MCP | Real stdio and Streamable HTTP clients discover tools, persist sessions during a process, reconnect, and avoid blind replay after ambiguous failures. |
| Research workflow | Python state rules enforce decomposition, proof-step dependencies, two verifier gates, regulation, and completed-method-only Summary selection. |
| Observability | TraceStore records metadata, workflow events, snapshots, and evaluation summaries; the Trace Viewer exposes a CLI. |
| Benchmark | Four deterministic offline math cases exercise routing, task dependencies, failure exclusion, revision, and snapshot restoration. |

## 4. Final Test Result

Command:

```powershell
python -m unittest discover -s tests -v
```

Final Phase G Windows result: **403 total; 400 passed; 0 failed; 0 errors; 3 skipped**. The command finished with exit code 0 and `OK (skipped=3)`.

| Skipped test | Recorded reason |
| --- | --- |
| `test_process_sandbox.ProcessSandboxTests.test_posix_backend_does_not_overclaim_process_count_limit` | POSIX backend capability |
| `test_process_sandbox.ProcessSandboxTests.test_posix_memory_limit_is_applied_to_child` | POSIX `prlimit` readback test |
| `test_sandboxed_command_executor.SandboxedCommandExecutorTests.test_explicit_workspace_executable_is_not_trusted` | POSIX executable bit test |

The skips are platform-specific and were not changed to improve the count.
The separate `py_compile` smoke check passed for `s20.py`, the workflow modules, observability store/evaluation, and benchmark runner.

## 5. Final Benchmark Result

Command:

```powershell
python benchmarks/run_benchmark.py --mode offline
```

Final result: **4 cases; 4 passed; 0 failed**. Every case's checklist passed.

| Case | Purpose | Final result |
| --- | --- | --- |
| `case_01` — elliptic maximum principle | Easy literature → direct-proof route and artifact | PASS |
| `case_02` — principal Neumann eigenvalue | Decomposition, durable dependent proof steps, Structural and Detailed gates | PASS |
| `case_03` — reaction–diffusion stability | Two methods, one injected task failure, completed-method-only Summary | PASS |
| `case_04` — Dirichlet bifurcation | Structural CONTINUE, Regulator REVISE_PROOF, durable revision task, both DONE gates | PASS |

The runner uses real coordinator/orchestrator, TaskStore, TraceStore, and ResearchEvaluation paths with deterministic fixture decisions.
It requires no Anthropic key, network call, or remote MCP server; the offline no-network test is part of the suite.
Fixture `proof.md` files explicitly state that they are **not mathematical proofs**.
Model/tool/token/cost fields are `null` offline because those quantities are not measured.
The non-interactive live benchmark entry point is not wired.

## 6. Workflow Verification

- **Easy:** Literature classification → Direct Proof → Completed is exercised by `case_01`.
- **Medium/hard:** Literature → candidate methods → Decomposition → TaskStore proof-step DAG → dedicated prover binding → Structural → Detailed → Completed is exercised by cases 2–4 and workflow tests.
- **Revision:** A separate `case_04` run recorded Structural CONTINUE = 1, proof revisions = 1, Structural DONE = 1, Detailed DONE = 1. Its trace orders Structural CONTINUE → Regulator REVISE_PROOF → revision-task synchronization → Structural DONE → Detailed DONE. The durable revision task ended `completed`.
- **Failure:** `case_03` records one failed method; the Summary checklist selects only a completed method.
- **Gate authority:** `s20.py` binds the `research_workflow` tool to the orchestrator; the coordinator rejects illegal stage changes. A Structural DONE cannot skip Detailed, and Summary requires completed methods.

These checks establish control-flow behavior, not the truth of model-submitted verifier verdicts.

## 7. Safety Verification

The sanity scan found no executable `shell=True` path, no `tar.extractall()` path, and no model-code `exec()` in the Agent host process.
The remaining `exec()` is in the restricted SymPy child worker.
The command executor parses one allowlisted command and uses `shell=False`.
Capability policy controls ALLOW/DENY/ASK decisions.
SymPy input is bounded and checked in a child process; Windows Job Objects and POSIX limits constrain child resources.
Archive extraction validates members rather than calling `extractall`.
MCP server definitions live in trusted configuration outside the agent-writable workspace.
The repository scan found no apparent committed credential value; environment-variable names and test placeholders remain.

These are layered restrictions, not a full OS filesystem jail or network namespace.
Configured MCP servers are trusted external integrations, not confined by the SymPy worker.

## 8. Durability / Recovery Verification

SQLite persists tasks and dependencies, jobs and schedules, unread team messages, scoped memory, workflow snapshots, and trace events.
The orchestrator reloads ResearchRun and method/task bindings from snapshots; TaskStore remains authoritative for durable task status on a later sync.
The offline benchmark reconstructs the orchestrator and checks that its fixture does not duplicate tasks or teammate spawns.
Lease recovery can make expired tasks claimable again.

A restart does **not** recreate a lost live teammate thread or MCP socket/session.
MessageBus marks a message consumed before business processing, so a crash can lose that processing.
TaskStore and TraceStore commit separately; a crash between task creation and snapshot save can require reconciliation before repeating method activation.
External effects are not covered by local database transactions.

## 9. Observability

`TraceStore` records runtime/model/tool metadata, policy decisions, task transitions, workflow events, and recovery snapshots.
`ResearchEvaluation` combines the current workflow snapshot with event history; past verifier verdicts remain countable after a revision clears current verdict fields.
Estimated model cost is available only with external pricing configuration.
Ordinary runtime trace producers use metadata, but workflow snapshots can include problem text, literature notes, proof goals, and regulator notes.
Treat `traces.db` as potentially sensitive local state.

The Phase G Trace Viewer smoke test ran `python -m agent_runtime.observability.viewer --help` and confirmed `--db`, `--run`, `--research-run`, `--recent`, and `--json`.
A read-only `--research-run benchmark_case_04 --json` invocation against the generated Case 4 trace DB exited 0 and returned stage `completed`.
No synthetic runtime model trace was created for this check.
The viewer's JSON is the evaluation object itself; it is not wrapped in an `evaluation` field.

## 10. Files Added / Major Files Changed

Important changes across the accepted engineering cycle:

| Domain | Main files |
| --- | --- |
| Research workflow | `agent_runtime/research/workflow.py`, `workflow_models.py`, `orchestration.py`, integration wiring in `s20.py` |
| Runtime and services | Lead/teammate runtime and application modules, tool catalog, task runtime, policy pipeline |
| Observability | `agent_runtime/observability/store.py`, `evaluation.py`, `viewer.py`, associated runtime hooks |
| Benchmark | `benchmarks/run_benchmark.py`, four `cases/*.json`, four `expected/*.json`, `benchmarks/README.md` |
| Tests | `tests/test_research_workflow_orchestration.py`, `test_research_workflow_persistence.py`, `test_observability.py`, `test_benchmark.py`, and updated catalog coverage |
| Documentation | Root `README.md`, `docs/architecture_audit.md`, `docs/architecture.md`, `docs/resume_project.md`, this report |

Phase G itself changed only `.gitignore`, `README.md`, `docs/architecture.md`, and this report.
No production code was changed during Phase G.
The historical `architecture_audit.md` retains its earlier 399/400-test baseline as a dated audit record; the current count appears here and in the README.

## 11. Known Limitations

1. Built-in process controls provide neither a full network namespace nor a complete filesystem jail.
2. Workflow snapshots and TaskStore tasks are not one cross-database transaction.
3. MessageBus is consume-on-read and has a consume-before-processing crash window.
4. Restored snapshots do not reconstruct live teammate threads or live MCP sessions.
5. Scheduler, teammate, and background worker shutdown is not uniformly structured on CLI exit.
6. A non-interactive live benchmark entry point is not wired; only offline workflow fixtures are automated.
7. Two model-mediated verifier stages and optional symbolic checks do not formally guarantee mathematical correctness.
8. Workflow snapshots can contain sensitive research text; TraceStore is not a universal redactor.
9. Context token counting is heuristic; estimated cost depends on external pricing settings.
10. Cron prompt delivery and external tool side effects do not have end-to-end exactly-once semantics.

## 12. Deferred Improvements

- Add explicit reconciliation or transactional intent for workflow activation across TaskStore, snapshots, and worktrees.
- Introduce claim/ack MessageBus handling with idempotent consumers where delivery guarantees are required.
- Restore or replace missing teammate workers from durable assignments and implement bounded CLI stop/join ownership.
- Evaluate stronger OS isolation appropriate to Windows and POSIX, with explicit filesystem and network guarantees.
- Add a non-interactive live evaluation path and independent mathematical review or proof-assistant integration.

These are future work, not part of this accepted implementation.

## 13. Manual Smoke-Test Checklist

Checked items were actually executed during Phase G. Unchecked items require a later interactive session or external server.

- [ ] Activate a fresh `.venv` and install dependencies.
- [x] Compile `s20.py` and key workflow, observability, and benchmark modules.
- [x] Run the full unit suite.
- [x] Run all four offline benchmark cases.
- [x] Run Case 4 separately and inspect its revision task and event sequence.
- [ ] Start `python s20.py` to the interactive prompt.
- [ ] Ask a simple non-research question.
- [ ] Start a live mathematical research workflow.
- [ ] Confirm the live literature stage.
- [ ] Confirm live method decomposition.
- [ ] Confirm live TaskStore DAG execution.
- [ ] Confirm a live prover teammate.
- [ ] Confirm live Structural verification.
- [ ] Confirm live Detailed verification.
- [ ] Confirm a live Regulator revision path.
- [ ] Confirm a real mathematical `proof.md` artifact.
- [x] Inspect Case 4 traces with the Trace Viewer help and read-only research-run command.
- [ ] Optional external MCP stdio smoke test.
- [ ] Optional external MCP Streamable HTTP smoke test.

The interactive CLI was not launched in this acceptance run: importing `s20.py` constructs an Anthropic client and runtime services, and a no-request CLI session was optional.
No paid Anthropic call or external MCP request was made.

## 14. Resume Readiness

The repository now has a GitHub-facing README, detailed architecture document, historical architecture audit, four-case offline benchmark, resume/interview guide, and passing automated suite.
The workflow and failure semantics can be demonstrated from code and trace evidence without an API key.
It is suitable as an engineering portfolio and interview project.
That assessment does not imply production deployment readiness or certified mathematical proofs.

## 15. Final Verdict

The full Windows unit suite passes with three documented POSIX-only skips.
All four offline workflow benchmark cases pass; the separate Case 4 run confirms the revision path in both trace history and a completed durable revision task.
The Trace Viewer CLI works against a generated research trace, and key entry points compile.
The architecture and documentation are consistent with those observed behaviors after the Phase G fact sync.
Remaining limits concern cross-store reconciliation, OS isolation, message delivery, worker lifecycle, live evaluation, and mathematical correctness.

**Acceptance decision: complete for the current engineering cycle and portfolio presentation, subject to the explicit limitations above.**

## 16. Repository Hygiene and Commit Plan

The working tree contains uncommitted code, tests, benchmark source, and documents from several accepted phases.
The latest committed history already includes the hard workflow state machine and earlier MCP/message/security work; this plan stages only the current working-tree changes.
The final scan found no tracked `.env`, `.venv`, `.state`, `.tasks`, `.worktrees`, `.mailboxes`, `.transcripts`, benchmark results, or SQLite trace DB.
Generated `benchmarks/results/` and policy `.audit/` are ignored; benchmark `cases/`, `expected/`, runner, and README remain versionable.
The versionable-file credential scan reported only code-variable usage and a test fixture, with no apparent real secret value.
Do not commit generated timestamp results or local runtime state.

A **single atomic feature commit** is the simplest plan because the current `s20.py` and runtime wiring reference newly added observability and orchestration modules.
Splitting the uncommitted code by domain could leave an intermediate commit that does not import cleanly.
No commit was created during acceptance.

After reviewing the changes, the repository owner can run:

```powershell
git status --short
git diff --check
git add .gitignore README.md s20.py agent_runtime benchmarks docs tests
git diff --cached --check
git diff --cached --stat
git status --short
git commit -m "feat: finalize research agent runtime and acceptance"
```

`git add` respects the ignore rules for `benchmarks/results/` and `.audit/`.
Review the staged diff before the final command, especially if other edits are made after this report.
