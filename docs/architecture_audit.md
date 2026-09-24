# Architecture Audit

Audit date: 2026-09-23. Scope: the current workspace, including the in-progress 12A/12B/12C implementation. This is a source and unit-test audit, not a live Anthropic or external MCP integration certification. Phase A established a 399-test baseline. After the narrow evaluation fix described below, the full suite reports 400 tests, OK, 3 skipped on Windows.

## Current Architecture

- s20.py is the composition root. It constructs the Anthropic client, SQLite stores, execution tracker, policy, tool registry, research workflow, MCP runtime, and CLI. Construction has side effects, including store initialization, legacy migration, lease recovery, and scheduler startup.
- agent_runtime/application contains the Lead and teammate research prompts and context assembly. agent_runtime/runtime contains the Lead, subagent, teammate, model gateway, and execution models. agent_runtime/research/workflow.py contains the pure legal state transitions; research/orchestration.py binds them to TaskStore, teammates, worktrees, snapshots, and trace events.
- agent_runtime/tools, policy, tasks, jobs, memory, context, team, security, and worktree own the respective runtime services. SQLite, subprocess/OS controls, Anthropic, and MCP are reached through these services or through the composition root.

## Verified Boundaries

The intended Application → Runtime / Orchestration → services → persistence/OS/Anthropic/MCP direction is substantially present. The application modules do not own SQLite or process execution. The coordinator has no model or I/O calls. The orchestrator depends on narrow callables supplied by s20.py. The CLI delegates agent execution rather than implementing a second agent loop. The inspected module import graph has no obvious cycle; the complete unit suite imports the modules successfully.

This is a practical boundary, not a fully enforced package architecture: context/runtime.py imports MemoryStore, research/tools.py imports security, and the composition root knows concrete services. Those are visible cross-service dependencies, not evidence of a second workflow controller.

s20.py still has many aliases for registry handlers and callbacks, plus small UI/adaptation helpers. Task, job, memory, team, model-loop, and research transition implementations are in modules. No duplicate old JSON task or mailbox runtime was found on the production path. The JSON task and JSONL mailbox readers are one-time migration paths into SQLite. Some compatibility aliases appear unused by the current composition root, but no wrapper was removed without proving external callers cannot use it.

## Runtime / Orchestration

AgentRuntime, SubagentRuntime, and TeammateRuntime share ExecutionTracker records. AnthropicModelGateway is the configured model gateway; this audit found no alternate model-provider path. Static Tool Catalog schemas are bound to concrete handlers in s20.py; policy hooks run before tool execution. The single tool-facing research_workflow handler points to ResearchWorkflowOrchestrator.dispatch. No production tool handler was found calling ResearchWorkflowCoordinator directly to bypass dispatch.

The Lead and teammate prompts restate the workflow for guidance. They explicitly make the Python workflow and durable tasks authoritative; no conflicting prompt-only transition implementation was found. Structured responses from decomposers, reviewers, and regulators still require the Lead to submit them through the workflow tool. Model reasoning and verdict quality are not guaranteed by the state machine.

## Durable State

TaskStore uses SQLite transactions with BEGIN IMMEDIATE for claims and dependency checks. Proof-step tasks are created in topological order with blockedBy task IDs and role affinity. Leases, heartbeats, and expired-lease recovery are present. Legacy unleased in-progress tasks are deliberately not guessed dead. JobStore persists statuses and retry attempts. Background tool jobs use max_attempts=1; interrupted running jobs are failed rather than automatically replaying possible side effects. Queued, not-yet-claimed jobs may be started later.

MessageBus uses MessageStore on the production path. Sending and read-and-consume are transactional, and old JSONL inboxes are imported idempotently. It is not a claim/ack queue: a crash after consume commits but before the consumer processes the returned message can lose delivery. Team protocol request state is process-local.

The inspected TaskStore, JobStore, MessageStore, MemoryStore, and TraceStore normal operation paths close each SQLite connection in a context manager or finally block. The test suite includes file-release checks. This does not prove every exceptional startup/OS path is immune to Windows WinError 32.

## Security

Source scans found no executable shell=True call. SandboxedCommandExecutor parses one allowlisted command, rejects shell syntax/interpreters, and uses subprocess.Popen with shell=False. The only Python exec() found is in security/_sympy_worker.py, the separate constrained SymPy child process, not the Agent host process. SafeArchiveExtractor manually validates and copies regular members; no tar.extractall() execution path was found.

Capability policy returns ALLOW, DENY, or ASK and writes a best-effort audit. SafeSympyExecutor uses AST restrictions, bounded input/output/time, a minimal child environment, and ProcessSandbox. Windows Job Objects and POSIX resource limits constrain child resources/processes. Strict filesystem or network isolation requests fail closed when the backend lacks those capabilities. These controls do not make arbitrary execution a complete OS sandbox.

## MCP

MCP server definitions are loaded from trusted configuration outside the agent-writable workspace. A configured server name takes precedence over the Mock runtime; a failed real connection returns an error and does not silently connect the same name to Mock. Persistent clients support stdio and Streamable HTTP, tool discovery, reconnection, and close(). Failed or timed-out tool calls invalidate the connection and are not automatically replayed, since remote side effects are unknown.

Mock docs/deploy servers remain selectable by their own names when not shadowed by a configured real server, including when other real servers are configured. They are demo/fallback tools, not proof that an external service was contacted. MCPRuntime.close() is called from s20.py's main finally block; each persistent client signals and joins its worker with a bounded timeout.

## Mathematical Research Workflow

The hard transitions were checked in workflow.py, orchestration.py, and their tests:

| Gate | Verified behavior |
| --- | --- |
| Decomposition | A medium/hard literature outcome creates methods in DECOMPOSITION. activate_method calls set_method_plan, which accepts only that stage and validates a nonempty acyclic DAG. |
| Proof tasks | activate_method creates dependency-linked TaskStore tasks and a dedicated prover binding. sync_method maps durable task statuses back through coordinator start_step/complete_step. |
| Verification | All steps must complete before structural review. Structural DONE enters DETAILED_VERIFY; it cannot enter summary directly. Structural or Detailed CONTINUE enters REGULATION. |
| Regulation | REVISE_PROOF creates a durable revision task and returns through structural review; REVISE_PLAN and REWRITE clear the plan and return to decomposition. REWRITE also increments its counter. |
| Summary | begin_summary accepts only methods whose stage is COMPLETED, reached after Detailed DONE. |

The coordinator enforces stage legality, but it does not independently prove mathematical correctness. The workflow accepts reviewer verdicts submitted by the Lead. complete_summary records a nonempty artifact path; it does not check file existence or proof content. TaskStore dependencies gate claims, while the exposed complete_task operation does not independently authenticate a mathematical proof.

## Observability

ExecutionTracker emits bounded in-memory events and catches event-sink failures, so TraceStore runtime-event write errors do not fail the Agent loop. Task transition and policy trace callbacks also catch sink errors; workflow event/snapshot writes are best-effort and warn rather than changing a successful transition. TraceStore persists usage, latency, tool names/status, policy decisions, task transitions, workflow events, and snapshots. Estimated cost is NULL unless external pricing is configured.

Production runtime/model/tool event payloads observed in this audit use metadata rather than raw prompts, raw shell commands, API keys, or complete tool outputs. This is not a general redaction guarantee: TraceStore.record_runtime_event and record_workflow_event accept arbitrary caller payloads. More importantly, workflow snapshots intentionally store problem_ref, literature_notes, method rationales, proof-step goals, and regulator notes for recovery. If callers put a full user prompt or a secret in those fields, traces.db can contain that text. Treat traces.db as sensitive local state and do not publish it. The trace-store comments about excluding raw prompts apply to ordinary event metadata, not to recovery snapshots.

ResearchEvaluation previously counted Structural/Detailed verdicts from only the latest method snapshot. Revisions clear those fields, causing historical CONTINUE events to disappear from counts. It now counts recorded verdict events and falls back to snapshot fields for older/imported runs without those events. A regression test covers the historical CONTINUE case. Evaluation still reflects only data recorded in the trace store and current proof steps; it is not an external proof-quality grade.

## Restart / Recovery Semantics

ResearchWorkflowOrchestrator loads ResearchRun and method bindings from workflow_snapshots on construction. The persistence test verifies restart restores a run and proof-step task IDs. TaskStore remains the authoritative status source when sync_method is called after restart. No disappeared teammate thread is automatically recreated from a snapshot, and no reviewer result is reconstructed from a lost thread. A human/Lead must inspect status, recover leases as appropriate, and explicitly resume orchestration.

TaskStore and TraceStore use separate SQLite databases. activate_method can create several durable tasks, mutate the in-memory workflow, create a worktree, and spawn a teammate before dispatch saves the snapshot. A crash or failed snapshot write in that interval can leave durable tasks ahead of the restored workflow. Similarly, a task may complete before a later sync_method snapshot records the corresponding proof-step state. Recovery is therefore durable but not cross-store atomic; inspect TaskStore and workflow status before retrying an interrupted action. Blindly repeating activate_method can create another task generation.

Background running tool jobs are not automatically replayed after their one allowed attempt. MCP failed calls are not replayed. Cron scheduled-prompt delivery has a different tradeoff: deliver_claimed_once marks a prompt job succeeded before calling the Agent loop, so a crash in between can lose that prompt; it should not be described as exactly-once delivery. The scheduler and autorun loops are daemon threads without a unified stop/join path on normal CLI exit. Background jobs and teammates likewise have no explicit CLI-wide drain/cancel step. Process exit ends those threads, but graceful completion is not guaranteed.

## Known Limitations

- A Windows Job Object is not a network namespace. The built-in process backends do not provide a full OS filesystem jail. Worktree/path checks and rebased environment variables are narrower controls.
- Context token counting is heuristic, not exact Anthropic provider token counting.
- MessageBus is consume-on-read, not claim/ack; it has a post-consume crash window. Team protocol requests are not durable across restart.
- Workflow snapshots restore data and bindings, not live teammate threads. Cross-database workflow/task operations are not atomic.
- Mock MCP is retained for fallback/demo names. A configured real server of the same name is not silently replaced by Mock.
- Model cost requires external TRACE_MODEL_PRICING_JSON values and is an estimate, not permanent built-in pricing.
- Trace snapshots hold mathematical work content and may hold sensitive text supplied in workflow fields. TraceStore is not a generic secret-redaction layer.
- Final proof artifact paths are recorded but file existence and mathematical validity are not automatically proven by the coordinator.
- The CLI does not explicitly stop/join every scheduler, teammate, and background worker thread.

## Technical Debt

1. Define an explicit recovery/reconciliation protocol across workflow snapshots, TaskStore tasks, and worktree creation before claiming automatic crash-safe method activation. Preserve the no-blind-replay rule for possible side effects.
2. Add a CLI lifecycle owner with bounded stop/join semantics for scheduler, cron autorun, teammate, and background workers. Do not treat daemon exit as graceful shutdown.
3. Consider claim/ack semantics for team messages and durable protocol requests if at-least-once delivery is needed.
4. Separate recovery snapshots from exportable telemetry or document/enforce access control and retention for traces.db. Keep runtime event payloads on an explicit safe-field allowlist if new producers are added.
5. Review cron delivery acknowledgment timing and idempotency before promising at-least-once or exactly-once scheduled execution.
6. Keep compatibility aliases and one-time migrations until callers and upgrade paths are inventoried; do not remove them for appearance alone.

## Audit Result

The core layering and mathematical state-machine gates are present. No executable shell=True, host-process model-code exec(), unsafe tar.extractall(), configured-real-to-Mock MCP fallback, or production bypass of ResearchWorkflowCoordinator was found. One concrete observability counting bug was fixed with a focused test; no broad refactor was performed. The main remaining risks concern cross-store crash reconciliation, sensitive workflow snapshots, message-delivery semantics, and incomplete graceful shutdown. After the production change, python -m unittest discover -s tests -v reports 400 tests, OK (3 skipped: POSIX-only checks on Windows).
