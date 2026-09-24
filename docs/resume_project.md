# ResearchAgent Runtime — Resume & Interview Guide

This guide reflects the repository on 2026-09-23. It is interview preparation, not a claim of formal proof correctness or production certification.
The current Windows acceptance baseline is 403 tests: 400 passed, 3 POSIX-only skips. All four offline benchmark cases pass.
Those are coverage counts, not latency, throughput, or mathematical accuracy results.

## Project Positioning

ResearchAgent Runtime is a durable multi-agent orchestration runtime for mathematical research.
It combines Python-enforced research stages, SQLite-backed tasks and coordination, constrained tool execution, real MCP connectivity, and trace-based workflow evaluation.
LLM agents propose mathematical methods and review results; the Python state machine controls legal transitions.

## One-Line Resume Version

**ResearchAgent Runtime**

Built a durable multi-agent runtime for mathematical research with Python-enforced proof workflows, SQLite task orchestration, constrained execution, MCP integration, and trace-based evaluation.

The one-line description is 22 words.

## Three Resume Bullets

- Designed a Python-enforced mathematical research workflow with method-specific decomposers and provers, dependency-ordered proof tasks, two verifier stages, and regulator revision loops.
- Built SQLite-backed task, job, and message services with atomic task claims, leases and recovery; added capability-based tool policy and child-process SymPy execution with resource limits.
- Integrated persistent MCP stdio and Streamable HTTP clients, workflow snapshots, trace evaluation, and four offline benchmark cases that exercise verification, revision, failure, and restart paths.

These bullets describe implemented mechanisms rather than measured performance.
“Four offline cases” means deterministic control-flow fixtures, not four independently verified mathematical proofs.

## 30-Second Version

Mathematical research agents need to coordinate long-running work, but a prompt-only loop can lose task state or skip verification. I built ResearchAgent Runtime to put those responsibilities in Python and SQLite while leaving mathematical reasoning to specialist LLM agents. A Lead proposes methods, each method gets a decomposer and prover, and proof steps run through a dependency graph before structural and detailed review. A regulator routes failed reviews back to proof revision or decomposition. The runtime also constrains tools, connects to MCP servers, and records recovery snapshots and traces. I validate the control flow with 403 unit tests and four offline benchmark cases; those tests do not certify theorem correctness.

## 2-Minute Version

I started with an interactive mathematical agent whose central script had to do too much: model calls, tool dispatch, research instructions, and stateful coordination. That works for a short answer, but a research problem can branch into candidate methods, dependent proof steps, and several rounds of review. If the process exits or a teammate fails, a transcript alone is not reliable execution state.

The project grew into a runtime that separates reasoning from control. LLM agents choose methods, draft plans, write proof text, and return structured reviewer findings. A Python `ResearchWorkflowCoordinator` enforces legal stages. A separate orchestrator creates TaskStore tasks for the proof-step DAG, assigns a prover per method, and starts structural, detailed, and regulator roles. Structural DONE leads to Detailed review. CONTINUE goes to a regulator, which can request proof revision, plan revision, or a rewrite. Summary can select only completed methods.

For durability, TaskStore uses SQLite transactions for atomic claims and dependency checks, with leases, heartbeats, and expired-lease recovery. Jobs, team messages, memory, snapshots, and traces have their own persistence paths. Tool execution is gated by capability policy. Generated SymPy code runs in a restricted child process; command execution uses an allowlist and `shell=False`. These reduce risk, but they are not a full network or filesystem sandbox.

The runtime connects to real MCP stdio and Streamable HTTP servers. It reconnects broken sessions without blindly replaying a call whose remote side effect is uncertain. TraceStore records workflow history and current snapshots. A four-case offline benchmark checks gates, dependencies, revisions, failure handling, and snapshot restoration without Anthropic access. The engineering lesson is that agent control flow must be explicit, and recovery claims must say where transactions end.

## 5-Minute Deep Dive

Use this as an outline, not a memorized script. Spend most time on the workflow, task transaction, and recovery boundary.

### 1. Problem

- **What to say:** A theorem-style question may need literature context, several approaches, proof dependencies, and revision after review.
- **Show:** The research workflow diagram in `README.md`.
- **Likely follow-up:** “Why can’t the Lead keep that in its prompt?” Prompts are advisory; status and legal transitions need code and storage.

### 2. Architecture

- **What to say:** `s20.py` composes application prompts, runtime loops, services, and external boundaries.
- **Show:** The layer diagram in `docs/architecture.md` and the construction area of `s20.py`.
- **Likely follow-up:** “Is the composition root still large?” Yes; it retains wiring and adapters, while service logic is modular.

### 3. Hard Mathematical Workflow

- **What to say:** The coordinator owns legal transitions; the orchestrator connects them to teammates, tasks, and traces.
- **Show:** `agent_runtime/research/workflow.py` and `agent_runtime/research/orchestration.py`.
- **Likely follow-up:** “Could Structural DONE go straight to Summary?” No; it enters Detailed verification.

### 4. Durable State

- **What to say:** Task claims and dependencies use SQLite transactions; leases help recover abandoned ownership.
- **Show:** `TaskStore.claim_task` and the durability matrix in `docs/architecture.md`.
- **Likely follow-up:** “What does a lease guarantee?” Reclaimable ownership, not rollback of external side effects.

### 5. Safe Execution

- **What to say:** Policy and executors are separate boundaries; SymPy runs outside the Agent process.
- **Show:** `agent_runtime/security/sympy_executor.py` and `command_executor.py`.
- **Likely follow-up:** “Is this a complete sandbox?” No network namespace or complete filesystem jail is provided.

### 6. MCP

- **What to say:** Trusted config selects real stdio or Streamable HTTP servers; the client discovers tools and keeps a session.
- **Show:** `agent_runtime/research/mcp.py` and README config examples.
- **Likely follow-up:** “What happens after a disconnect?” Reconnect for later calls; do not replay an ambiguous earlier call.

### 7. Observability

- **What to say:** Events explain history; snapshots restore current workflow state and bindings.
- **Show:** `agent_runtime/observability/store.py`, `evaluation.py`, and Trace Viewer.
- **Likely follow-up:** “Are traces safe to publish?” No; snapshots can contain problem text and regulator notes.

### 8. Benchmark

- **What to say:** Four offline fixtures exercise control flow without model calls or network access.
- **Show:** `benchmarks/README.md` and case 4's CONTINUE → REVISE_PROOF → DONE route.
- **Likely follow-up:** “Does PASS mean the theorem was proved?” No; it verifies routing and storage gates.

### 9. Limitations

- **What to say:** Name message consume loss, cross-database consistency, lost teammate threads, and CLI shutdown debt.
- **Show:** `docs/architecture_audit.md`, especially Restart / Recovery Semantics.
- **Likely follow-up:** “What would you fix first?” Reconciliation and lifecycle ownership.

## How the Project Evolved

This is the design evolution supported by the current modules and retained compatibility paths. It is not a claim that every item landed in the exact calendar order shown.

1. **Interactive entry point:** `s20.py` remains the composition root. More tools and collaborators made one broad loop hard to inspect.
2. **Tool registry:** Stable names, schemas, and dispatch points made policy and trace hooks possible.
3. **Capability policy:** `ALLOW`, `DENY`, and `ASK` became execution decisions instead of prompt suggestions.
4. **SQLite TaskStore:** JSON task files were weak for concurrent claims and dependency transactions; they remain migration inputs.
5. **Jobs and scheduling:** Queued/running/final states and retry budgets made background work inspectable without blind side-effect replay.
6. **Context and memory:** Compaction, checkpoints, transcripts, and scoped records supported longer work; token counting remains heuristic.
7. **Application/runtime split:** Prompt and context assembly separated from model loops, records, and execution mechanics.
8. **Teammates and messages:** Candidate methods retained distinct working context; durable SQLite messages replaced active JSONL mailboxes.
9. **Safe execution:** Model-generated symbolic code moved to a restricted child; commands gained allowlists and no-shell invocation.
10. **Real MCP:** Demo Mock servers were insufficient evidence of external integration; trusted config now supports stdio and Streamable HTTP.
11. **Hard workflow:** Prompt prose could request reviews but not enforce them; Python gates and durable proof-step tasks now do.
12. **Observability:** Events and snapshots reveal history and recovery state; historical verdict counts come from events because revisions clear current verdicts.
13. **Offline benchmark:** Deterministic cases exercise the control plane; live mathematical quality evaluation remains separate.

Each boundary addressed a concrete state, safety, or inspection need. Do not present the sequence as a measured reliability gain.

## Difficult Engineering Decisions

### A. Why SQLite instead of JSON files?

A task claim must check status, ownership, and dependencies before changing ownership.
With separate JSON reads and writes, concurrent teammates can both observe the same pending task or leave a partial update.
`TaskStore.claim_task` uses `BEGIN IMMEDIATE` so the check and update occur in one SQLite transaction.
Leases and heartbeats retain enough state to reclaim an expired claim after a crash.
SQLite connection closure is also easier to reason about than ad hoc open files on Windows, though no approach eliminates every WinError 32 path.
Legacy JSON reading remains for migration, not active orchestration.
**Boundary:** SQLite gives local durability, not distributed consensus.
**Follow-up:** Reclaiming a lease may duplicate an earlier external side effect; inspect or use idempotency for such work.

### B. Why a hard workflow instead of prompts?

A prompt can request literature survey, decomposition, and two reviews, but the model may misread it or lose it after compaction.
The coordinator encodes legal stages and rejects an invalid transition regardless of model wording.
Structural DONE enters Detailed review, and Summary accepts only completed methods.
The model still chooses methods and evaluates proof content; the state machine controls sequence.
**Trade-off:** A new workflow variant requires code and tests, not just a prompt edit.
**Boundary:** A legal sequence cannot establish mathematical truth by itself.
**Follow-up:** A false reviewer DONE remains possible and calls for independent review.

### C. Why one prover per method?

Competing methods may use different operators, norms, notation, normalizations, and partial lemmas.
A dedicated prover keeps its method's context coherent across the proof-step DAG and revision cycle.
Methods can advance independently, while steps inside one method obey TaskStore dependencies.
A shared prover could import an assumption from the wrong approach.
**Trade-off:** More teammates may increase model calls and token/API cost; no cost optimum has been measured.
**Boundary:** Method-level parallelism does not mean every step runs concurrently.
**Follow-up:** Shared lemmas should become explicit reviewed artifacts, not implicit context leakage.

### D. Why separate Coordinator and Orchestrator?

`ResearchWorkflowCoordinator` validates pure domain transitions without database, model, filesystem, or thread calls.
`ResearchWorkflowOrchestrator` applies those transitions to TaskStore, teammates, worktrees, events, and snapshots.
The coordinator can be tested with small deterministic inputs.
The orchestrator can use fake callbacks or local stores without claiming model proof quality.
**Trade-off:** There is a mapping from ProofStep IDs to durable task IDs to maintain.
**Boundary:** Its effects and snapshot writes are not a cross-database transaction.
**Follow-up:** A new reviewer stage needs both a legal coordinator transition and orchestrator effects/tests.

### E. Why are failed MCP calls not automatically replayed?

A timeout means the client did not receive a result; it does not prove the server did not act.
Retrying a deployment-like tool could duplicate an external side effect.
The persistent client invalidates a broken session and can reconnect later.
Connection recovery is different from repeating the ambiguous request.
**Trade-off:** The caller may need to inspect remote state before deciding whether to call again.
**Boundary:** The runtime has no generic exactly-once protocol for external MCP operations.
**Follow-up:** Retry is safer with a documented idempotent tool or server-supported idempotency key.

### F. Why does SymPy code run in a child process?

Symbolic programs may originate from a model; running them in the long-lived Agent interpreter exposes its state directly.
`SafeSympyExecutor` sends bounded input to `_sympy_worker.py`.
The worker applies AST restrictions and a curated namespace.
The parent supplies a temporary directory, minimal environment, timeout, output bounds, and OS resource controls.
AST validation is useful but should not be the only boundary.
**Trade-off:** Process startup adds overhead and constrains allowed symbolic programs.
**Boundary:** Windows Job Objects and POSIX limits do not provide a full network or filesystem sandbox.

### G. Why keep both events and snapshots?

A snapshot answers “what state and bindings can I restore now?”
Events answer “which transitions and verdicts happened, and in what order?”
A revision can clear the current Structural or Detailed verdict fields.
`ResearchEvaluation` therefore counts historical verifier events when available, preserving a previous CONTINUE.
Snapshots still restore ResearchRun and ProofStep-to-task bindings efficiently.
**Trade-off:** Event and snapshot writes are separate and can briefly diverge after a crash.
**Boundary:** Recovery snapshots can contain research text and are not export-safe telemetry by default.

## Likely System Design Questions

### 1. Why not use LangGraph?

The goal was to make this mathematical state graph visible in ordinary Python and tests.
The coordinator directly encodes decomposition, proof, two reviews, regulation, and Summary eligibility.
A framework could be considered later, but it would not remove durable-effects and retry design.

### 2. Why not use Celery?

The current jobs and tasks run locally with SQLite state.
A distributed queue would add infrastructure and delivery semantics not required by the current deployment.
Side-effect replay and workflow/task consistency would still need explicit design.

### 3. Why not store everything in Redis?

The project uses local durable SQLite databases and transactional claims; no multi-host deployment requirement is established.
Redis could support shared state, but changing storage would not by itself solve external exactly-once effects or cross-store atomicity.

### 4. Why SQLite?

It gives local transactions without a separate database service.
`BEGIN IMMEDIATE` protects claim and dependency checks, while rows persist leases and statuses.
The limits are local scale, separate-database consistency, and careful connection lifetimes.

### 5. What if two agents claim the same task?

The claim transaction checks status and dependencies under a write lock.
Only one can change a pending task to in-progress; the other gets a non-claimed result.
This protects the row, not arbitrary external work.

### 6. What if an agent crashes after claiming?

A leased in-progress task can return to pending after expiry and recovery.
Legacy unleased in-progress tasks are not guessed dead.
Before redoing work, check whether a side effect may already have happened.

### 7. What if MessageBus crashes after consume?

`MessageStore.consume_inbox` marks messages consumed before the application finishes handling them.
A crash can lose the business action even though the message row remains marked consumed.
A future claim/ack design would acknowledge after processing and handle possible duplicates.

### 8. Is execution exactly-once?

No. Transactions are local to a store; external calls and the separate databases are not one transaction.
Lease recovery, cron delivery, MessageBus consumption, and MCP have different delivery windows.

### 9. How do you prevent the LLM from skipping review?

The coordinator refuses illegal transitions.
All proof steps must finish before Structural; Structural DONE enters Detailed; Summary selects completed methods only.
Prompts guide reasoning but cannot authorize a shortcut.

### 10. How do you restore after restart?

The orchestrator loads ResearchRun and method/task bindings from TraceStore snapshots.
TaskStore retains task and lease state; `sync_method` can read those statuses.
Teammate threads are not recreated automatically, and cross-store differences need inspection.

### 11. Why not retry MCP calls?

The server may have acted before the response was lost.
The client can reconnect for later calls but does not replay an ambiguous call.
The operator or caller must establish whether another invocation is safe.

### 12. How do you isolate generated code?

Restricted SymPy code runs in a child process with AST/namespace rules and time, output, and resource bounds.
The command executor uses a no-shell allowlist.
These are layered restrictions, not a complete OS jail.

### 13. Why not Docker for everything?

The runtime supports Windows without mandatory Docker.
Containers might strengthen a future deployment boundary but would add packaging and operations.
Current Job Object/POSIX controls are narrower and are described that way.

### 14. Why not make the whole Runtime async?

The code combines interactive flow, teammate/job threads, and an asyncio loop inside persistent MCP clients.
A broad async conversion would not solve task durability or stage legality.
Thread shutdown remains debt regardless of interface style.

### 15. Why only Anthropic?

The supported model gateway is Anthropic.
A provider abstraction was not added solely for appearance.
A second provider would require tested handling for tool schemas, usage, errors, and behavior differences.

### 16. Why no embedding-based memory?

The repository has scoped memory, transcripts, checkpoints, and compaction.
The current workflow uses explicit tasks and artifacts; embedding retrieval was not needed to validate its control plane.
Measure a retrieval bottleneck before adding another stateful service.

### 17. How do you evaluate mathematical agents?

The offline benchmark checks workflow completion, methods, TaskStore dependencies, reviewer gates, revisions, failure exclusion, snapshots, and artifact existence.
Traces add event and usage data when available.
This evaluates orchestration, not a live-model success rate or theorem truth.

### 18. How do you know the proof is correct?

The runtime cannot guarantee that.
It enforces Structural and Detailed reviews and can perform constrained symbolic checks, but reviewer judgments and proof text remain fallible.
Human mathematical review or a proof assistant would provide stronger assurance; neither is claimed as an implemented formal guarantee.

## Failure Semantics

### Task claim crash

1. TaskStore holds an in-progress row with a lease.
2. The worker disappears; the lease eventually expires.
3. Recovery can return the task to pending.
4. The Lead must consider whether the old worker already produced an external side effect.
5. A lease repairs ownership, not remote state.

### Message consume crash

1. SQLite inbox consumption marks the message consumed in a transaction.
2. The caller receives the message and begins handling it.
3. A crash after commit but before handling leaves no unread message to retry.
4. Claim/ack with idempotent consumers is a future design, not current behavior.

### MCP disconnect

1. A transport failure invalidates the persistent client session.
2. A later operation can reconnect and rediscover tools.
3. The failed tool call is not automatically repeated.
4. Its remote side effect remains uncertain until checked with the server.

### Workflow snapshot crash window

1. TaskStore writes proof-step rows in `tasks.db`.
2. TraceStore writes events and snapshots in `traces.db`.
3. There is no atomic commit across tasks, worktree/teammate effects, events, and snapshots.
4. A crash can leave tasks ahead of restored workflow state.
5. Reconcile task IDs, statuses, bindings, and worktree artifacts before reactivating a method.

### Teammate thread crash

1. Durable tasks and workflow snapshots remain.
2. The actual Python teammate thread is gone.
3. Restart does not automatically rebuild it or recover an unrecorded reviewer answer.
4. The Lead must inspect state and explicitly resume or replace work.

### Background jobs and cron

- Interrupted side-effecting background tool work is not blindly replayed.
- Cron prompt delivery can mark a prompt job succeeded before the Agent loop receives it.
- A crash in that interval can lose delivery, so “exactly once” is not accurate.
- CLI exit does not uniformly drain and join all daemon workers.

## Security Interview Questions

### 1. Is this really sandboxed?

It has tool policy, allowlists, path checks, a SymPy child process, bounded time/output, and OS resource controls.
It is not a complete OS sandbox.
The built-in backends do not provide a network namespace or full filesystem jail.

### 2. Can an Agent run arbitrary shell?

`SandboxedCommandExecutor` parses one allowlisted command and launches it with `shell=False`.
It rejects shell chaining, redirection, substitution, and shell interpreters.
A permitted command is still real OS code, so allowlisting and policy matter.

### 3. Can SymPy code escape?

There is no absolute guarantee.
The worker rejects imports and dangerous introspection/file/process surfaces and runs separately under limits.
Strong network and filesystem isolation lie outside the built-in backends' guarantees.

### 4. What does a Windows Job Object protect?

It constrains configured child-process memory, CPU, and process-tree behavior.
It is not a network namespace or filesystem jail.
The architecture also notes that attachment occurs after process launch.

### 5. Is network isolated?

No complete network isolation is provided by the built-in process backends.
Strict unsupported isolation requests fail closed.
Trusted external MCP connections are intentionally network-capable.

### 6. Can MCP bypass the command sandbox?

A trusted stdio MCP server is external code launched from human-owned configuration; an HTTP server is a separate endpoint.
Those integrations do not inherit the command executor allowlist.
The boundary is trusted config, policy, and the server itself, not the SymPy worker.

### 7. Where do MCP credentials live?

Trusted `~/.researchagent/mcp_servers.json` can refer to credential environment variables.
The README HTTP example uses a bearer-token variable name instead of storing a token in the repository.
Environment and local config still need ordinary secret handling.

### 8. Why is trusted MCP config outside the workspace?

An Agent can edit project files through allowed tools.
If it could rewrite the trusted server command or endpoint, a project edit could alter external code execution.
`MCPRuntime` rejects a trusted config path inside the agent-writable workspace.

### 9. Are traces free of sensitive data?

No.
Normal runtime/model/tool producers use metadata rather than full prompts or tool outputs, but TraceStore accepts caller payloads.
Recovery snapshots can contain problem references, literature notes, goals, and regulator notes.

## Evaluation / Benchmark Questions

### Why not exact-answer matching?

A proof can use valid alternative formulations.
Exact strings can reject sound methods while missing a skipped review.
The benchmark checks legal transitions, task dependencies, revision routing, and artifact presence.

### Why not use only an LLM judge?

A judge can assess content but cannot by itself establish that a durable task waited for dependencies or that an illegal transition was rejected.
Judge quality is model-dependent.
The current benchmark uses deterministic state/storage evidence; live content grading would need separate validation.

### What does offline mode test?

It uses the real coordinator, orchestrator, TaskStore, TraceStore, and ResearchEvaluation.
Fixture teammates and scripted verdicts exercise direct proof, standard verification, failed-method exclusion, and proof revision.
It also reconstructs the orchestrator from snapshots without replaying tasks or spawns in those fixtures.

### What does offline mode not test?

It does not call Anthropic or an MCP server, grade proof text, measure live tokens/cost, or reproduce every crash timing.
Generated `proof.md` files say they are not mathematical proofs.
`--mode live` is not wired to a non-interactive Lead entry point.

### Why force CONTINUE → REVISE_PROOF → DONE in case 4?

A simple DONE path would leave the regulator branch untested.
Case 4 checks Structural CONTINUE, a durable regulator revision task, return to Structural, and eventual Detailed review.
It is a deterministic control-flow fixture, not evidence that a real bifurcation proof required correction.

### How should benchmark metrics be read?

Workflow and verifier counts come from actual trace/evaluation state in the fixture.
Offline model calls, tool calls, tokens, and estimated cost are `null` because no such usage is measured.
Reported latency is local runner wall time, not live model latency or a performance result.

## Interview Demo Plan

Target length: 5–8 minutes. Prepare the environment in advance; use the offline path so no credential or live-service latency is required.

1. **Minute 0–1: problem and architecture.**
   Open `README.md` and show the mathematical workflow plus architecture diagrams.
   Say that LLMs reason while Python enforces legal transitions.
2. **Minute 1–2: acceptance baseline.**
   Run `python -m unittest discover -s tests -v` if time permits, or show the verified 403-test result.
   Explain that the three Windows skips are POSIX-specific.
3. **Minute 2–3: benchmark.**
   Run `python benchmarks/run_benchmark.py --mode offline`.
   Point to four passing cases and inspect the case 4 checklist.
4. **Minute 3–4: trace and artifact.**
   Open one `result.json`, its `proof.md` warning label, and `traces.db` with the Trace Viewer.
   Explain that this tests workflow behavior, not theorem truth.
5. **Minute 4–5: hard gate.**
   Show `ResearchWorkflowCoordinator.record_structural_verdict` and `begin_summary`.
   Explain why Structural DONE cannot skip Detailed.
6. **Minute 5–6: durable work.**
   Show `TaskStore.claim_task` and the transaction around dependency checks.
   Mention lease recovery and its side-effect caveat.
7. **Minute 6–7: execution boundary.**
   Show `SafeSympyExecutor` starting `_sympy_worker.py` instead of running code in the Agent process.
8. **Minute 7–8: integration and limit.**
   Show `MCPRuntime` configured-real precedence and reconnect behavior.
   End with the cross-store snapshot window and next recovery improvement.

For a short slot: README diagram → offline benchmark → workflow state machine → traces.
Do not present the unsupported live benchmark as a working live evaluation.

## Code Walkthrough Order

1. **`s20.py`:** Show the composition root and where concrete services are wired.
   This establishes the runtime boundary before a low-level helper.
2. **`agent_runtime/research/workflow.py`:** Show legal mathematical stages and rejected transitions.
   The interviewer sees the central invariant first.
3. **`agent_runtime/research/orchestration.py`:** Show how a transition creates tasks, teammates, and trace writes.
   This maps abstract proof steps to execution.
4. **`agent_runtime/tasks/store.py`:** Show claim transaction and dependency enforcement.
   This grounds “durable” in a real write path.
5. **`agent_runtime/runtime/teammate.py`:** Show autonomous teammate execution and thread ownership.
   State the restart/shutdown limitation.
6. **`agent_runtime/security/`:** Show the SymPy worker and no-shell command executor.
   Separate application restrictions from OS resource controls.
7. **`agent_runtime/research/mcp.py`:** Show trusted config, real-client precedence, sessions, and no blind replay.
   Explain the external side-effect boundary.
8. **`agent_runtime/observability/`:** Show event/snapshot storage and evaluation.
   Finish with inspection and metric limits.

The order moves from composition → invariant → effects → durability → safety/integration → evidence.
If asked about memory or jobs, branch from `s20.py` into those modules rather than adding them to every walkthrough.

## Trade-offs

### SQLite versus a distributed database

- SQLite keeps local installation simple and gives transactions for claims and dependencies.
- It does not provide multi-host coordination or a cross-database transaction among tasks, traces, and external effects.
- A distributed database is justified only if deployment scale and operational needs warrant its complexity.

### Threads versus asyncio

- Teammates, jobs, cron, and MCP have distinct lifecycles; MCP uses a dedicated event loop thread.
- Threads fit existing blocking interfaces but need stop/join ownership, which the CLI lacks uniformly.
- Rewriting everything as async would not automatically fix durability or cancellation.

### State machine versus prompt flexibility

- Explicit transitions prevent stage skipping and make regression tests deterministic.
- A new scientific workflow needs code changes and tests.
- Mathematical creativity remains in agent proposals and proof text, not in bypassing gates.

### Method-level parallelism versus token/API cost

- Separate provers preserve notation and let independent approaches advance.
- More active agents may consume more calls and context.
- The repository does not establish a measured cost/quality optimum.

### Durability versus implementation complexity

- Tasks, jobs, messages, memory, events, and snapshots are inspectable after exit.
- Multiple stores introduce crash windows, migrations, and reconciliation work.
- “Durable” must be explained per subsystem, never as blanket exactly-once execution.

### Strict tool allowlist versus command flexibility

- A narrow executor reduces accidental shell composition and interpreter abuse.
- It can block legitimate experiments until reviewed and allowlisted.
- Policy, executor validation, and OS process limits address different risks.

### Observability versus privacy

- Events expose verifier history and runtime behavior; snapshots support restart.
- Snapshots can hold research text and need sensitive-data handling.
- Exportable telemetry would need separate minimization and retention rules.

## Limitations I Would Mention in an Interview

- **No full network namespace:** Windows Job Objects and POSIX limits do not isolate networking.
- **No complete filesystem jail:** Workspace/path checks constrain ordinary operations, not OS filesystem access.
- **MessageBus consume window:** A post-consume crash can lose processing; the bus is not claim/ack.
- **Cross-database consistency:** TaskStore and TraceStore commit separately during activation and sync.
- **No automatic teammate reconstruction:** Snapshots restore state and bindings, not Python threads.
- **CLI worker shutdown:** Scheduler, autorun, teammate, and background workers lack one unified stop/join owner.
- **Live benchmark entry point missing:** Offline fixtures pass, but `--mode live` is not connected to a non-interactive Lead run.
- **No formal proof guarantee:** Reviews and symbolic checks improve scrutiny but do not certify the theorem.
- **Trace sensitivity:** Workflow snapshots may contain user-supplied mathematical text.
- **Token/cost limits:** Context token estimates are heuristic; cost needs external pricing configuration.

**How I would improve it:** Start with workflow/task reconciliation, then claim/ack messaging and worker lifecycle ownership.
Evaluate a stronger platform-specific container/AppContainer boundary for isolation.
Add live evaluation with independent expert or proof-assistant review.
None of these improvements is presented as implemented.

## What I Would Build Next

1. **Workflow/task reconciliation:** Record an activation intent and reconcile task IDs, bindings, and worktree effects after interruption.
2. **Claim/ack team messages:** Keep a message claimable until handling is acknowledged, with idempotent consumers.
3. **Teammate lifecycle recovery:** Restore or replace missing workers from durable assignments and add bounded CLI stop/join.
4. **Stronger OS isolation:** Test Windows AppContainer/container and POSIX namespace profiles against explicit filesystem/network requirements.
5. **Live quality evaluation:** Add a non-interactive Lead benchmark and independent human or proof-assistant checks.

## STAR Interview Stories

### Story 1 — Concurrency and SQLite

- **Situation:** Research steps and teammate work needed durable dependencies and could be claimed concurrently.
- **Task:** Prevent two workers from taking one pending task while retaining restart state.
- **Action:** Moved active task records to SQLite, used transactional claim/dependency checks, and added leases, heartbeats, and recovery. Kept JSON import for migration.
- **Result:** Concurrency and recovery tests pass in the current 403-test suite. This demonstrates a local claim invariant; no throughput or crash-rate improvement was measured.
- **Lesson:** Durable ownership needs both a transaction and a plan for ambiguous side effects after lease expiry.

### Story 2 — Model-generated SymPy execution

- **Situation:** The Agent needed symbolic checks on expressions that could be generated by a model.
- **Task:** Keep that code out of the long-lived Agent interpreter and bound resource use.
- **Action:** Put execution in a restricted child worker with AST/namespace checks, temporary directory, minimal environment, timeout, output limits, and process resource controls.
- **Result:** Compiled generated code executes in `_sympy_worker.py`, not the Agent host process; relevant security tests pass. This reduces exposure without proving full sandboxing.
- **Lesson:** Static filtering helps, but process and OS boundaries must be stated separately.

### Story 3 — Prompt-only research workflow

- **Situation:** A multi-method proof needed literature, decomposition, dependency-ordered steps, two reviews, and revision.
- **Task:** Make review order and Summary eligibility deterministic despite variable model replies.
- **Action:** Added a pure coordinator for legal transitions and an orchestrator that maps ProofSteps to TaskStore tasks and specialist roles. Added tests and four offline cases.
- **Result:** The suite passes 403 tests with three Windows POSIX skips; all four offline cases pass, including a Structural CONTINUE → REVISE_PROOF → Structural DONE → Detailed DONE route.
- **Lesson:** Process correctness is testable separately from mathematical correctness; both require evidence.

## One-Sentence Answers

- **“What is the hardest part?”** Keeping workflow state, durable tasks, and external side effects consistent when their updates are not one transaction.
- **“What makes this different from a wrapper?”** It has enforced transitions, SQLite task and message state, controlled execution, specialist roles, and recovery data.
- **“Why is this an infrastructure project?”** It defines execution, ownership, policy, recovery, and observation contracts used by mathematical agents.
- **“What would you change first?”** Add explicit reconciliation for TaskStore tasks and TraceStore snapshots before claiming automatic crash-safe activation.
- **“How do you evaluate it?”** Unit tests and four offline cases verify gates, dependencies, revisions, and artifact presence; they do not grade theorem truth.
- **“What does safe mean here?”** Capability gates, constrained tools, and child resource limits reduce risk; full OS network/filesystem isolation is absent.
- **“Can it resume work?”** It restores workflow bindings and durable task status, but lost threads and cross-store mismatches need explicit handling.
- **“Why keep Mock MCP?”** It supports demos/fallback names; a configured real server of the same name wins and failed real connection does not silently become Mock.
- **“Why are offline usage metrics null?”** No model or runtime tool call is measured, so zero would confuse no measurement with measured usage.
- **“Can I trust the final proof?”** Treat it as a candidate artifact after two model-mediated reviews, not a formally certified theorem.

## Chinese Resume Version

**项目名称：ResearchAgent Runtime**

**一句话描述：** 构建面向数学研究的多智能体运行时，用 Python 状态机约束证明流程，并以 SQLite 持久化任务、受限工具执行和追踪评估支撑长任务协作。

**中文简历要点：**

- 设计数学研究硬流程：按候选方法分配 Decomposer 与 Prover，将证明步骤映射为有依赖关系的任务，并通过结构审查、细节审查和 Regulator 修订控制总结入口。
- 实现基于 SQLite 的任务、作业和消息持久化，支持原子领取、租约、心跳与恢复；加入能力策略、独立进程 SymPy 执行及无 Shell 命令限制。
- 接入持久化 MCP stdio / Streamable HTTP 客户端，建立工作流快照、事件追踪与评估汇总；用 4 个离线数学案例验证依赖、失败、审查与修订路径。

**30 秒中文介绍：**

这个项目解决的是数学研究 Agent 在长流程里容易丢失状态、跳过审查、以及多个方法互相干扰的问题。我把“怎么推进流程”交给 Python 状态机，把“怎么做数学推理”留给 LLM：每个候选方法有自己的分解与证明任务，步骤由 SQLite 依赖关系控制，完成后还要经过结构和细节两轮审查。运行时同时提供工具权限、受限执行、MCP 接入和可恢复的追踪快照。当前有 403 个单元测试和 4 个离线流程案例通过；这些验证的是工程流程，不代表系统能保证数学证明正确。

