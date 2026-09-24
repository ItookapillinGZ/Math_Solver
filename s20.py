#!/usr/bin/env python3
"""
s20: ResearchAgent Runtime composition root.

Run:  python s20.py
Need: pip install anthropic python-dotenv; real MCP also needs "mcp[cli]"

Runtime behavior lives under ``agent_runtime``.  This entrypoint constructs the
durable stores and runtime services, binds tool handlers, wires dependencies,
and starts the interactive CLI.
"""

import os, random
from dataclasses import replace
from pathlib import Path

from anthropic import Anthropic

from agent_runtime.config import (
    WORKDIR,
    MODEL,
    PRIMARY_MODEL,
    FALLBACK_MODEL,
    SKILLS_DIR,
    TRANSCRIPT_DIR,
    TOOL_RESULTS_DIR,
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    MAX_RETRIES,
    MAX_CONSECUTIVE_529,
    MAX_RECOVERY_RETRIES,
    BASE_DELAY_MS,
    CONTEXT_LIMIT,
    KEEP_RECENT_TOOL_RESULTS,
    PERSIST_THRESHOLD,
    CONTINUATION_PROMPT,
)
from agent_runtime.tools import (
    ToolExecutionConfig,
    ToolExecutionRuntime,
    build_basic_tool_registry,
)
from agent_runtime.tasks import (
    Task,
    TaskRuntime,
    TaskRuntimeConfig,
    TaskRuntimeDependencies,
    TaskStore,
    select_tasks_for_worker,
    task_matches_worker,
)
from agent_runtime.jobs import (
    BackgroundJobRuntime,
    BackgroundJobRuntimeDependencies,
    CronSchedulerConfig,
    CronSchedulerDependencies,
    CronSchedulerRuntime,
    JobStore,
)
from agent_runtime.policy import PolicyHookPipeline
from agent_runtime.context import (
    ContextRuntime,
    ContextRuntimeConfig,
    ContextRuntimeDependencies,
)
from agent_runtime.memory import (
    MemoryRuntime,
    MemoryRuntimeConfig,
    MemoryRuntimeDependencies,
    MemoryStore,
)
from agent_runtime.runtime import (
    AgentRuntime,
    AgentRuntimeConfig,
    AgentRuntimeDependencies,
    AnthropicGatewayConfig,
    AnthropicModelGateway,
    ExecutionTracker,
    SubagentRuntime,
    SubagentRuntimeConfig,
    SubagentRuntimeDependencies,
    TeammateRuntime,
    TeammateRuntimeDependencies,
)
from agent_runtime.application import (
    LeadAgentApplication,
    LeadAgentDependencies,
    ResearchTeamApplication,
)
from agent_runtime.team import MessageStore, TeamCoordinator
from agent_runtime.cli import (
    AgentCLIApplication,
    AgentCLIApplicationDependencies,
    TerminalEventRenderer,
)
from agent_runtime.worktree import (
    WorktreeRuntime,
    WorktreeRuntimeConfig,
    WorktreeRuntimeDependencies,
)
from agent_runtime.research import (
    MCPRuntime,
    ResearchToolRuntime,
    ResearchWorkflowCoordinator,
    ResearchWorkflowOrchestrator,
    ResearchWorkflowOrchestratorDependencies,
    SkillCatalog,
)
from agent_runtime.security import SafeSympyConfig, SafeSympyExecutor
from agent_runtime.observability import TracePricing, TraceStore

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

PROMPT = "\033[36ms20 >> \033[0m"
CLI_RENDERER = TerminalEventRenderer(PROMPT)


def terminal_print(text: str = ""):
    CLI_RENDERER.emit(text)


# Persistent observability. TraceStore records safe runtime metadata, model
# usage, workflow snapshots/events, permission decisions, and task transitions.
# Raw prompts, raw tool inputs, and full tool outputs are intentionally excluded.
TRACE_STORE = TraceStore(
    WORKDIR / ".state" / "traces.db",
    pricing=TracePricing.from_json(os.getenv("TRACE_MODEL_PRICING_JSON")),
)
EXECUTION_TRACKER = ExecutionTracker(
    event_sink=TRACE_STORE.record_runtime_event,
    max_events=2000,
)


def _trace_task_transition(payload: dict) -> None:
    TRACE_STORE.record_task_transition(
        {
            **payload,
            "runtime_run_id": EXECUTION_TRACKER.current_run_id(),
        }
    )


def _trace_policy_decision(payload: dict) -> None:
    TRACE_STORE.record_policy_decision(
        {
            **payload,
            "runtime_run_id": EXECUTION_TRACKER.current_run_id(),
            "agent_id": EXECUTION_TRACKER.current_agent_id(),
        }
    )


def _trace_workflow_event(event_type: str, research_run_id: str, payload: dict) -> None:
    TRACE_STORE.record_workflow_event(
        event_type,
        research_run_id,
        payload,
        runtime_run_id=EXECUTION_TRACKER.current_run_id(),
    )

# Symbolic verification is intentionally executed outside the Agent process.
# The child process receives only restricted SymPy code and is bounded by a
# wall-clock timeout; POSIX children also apply CPU/address-space rlimits.
SYMPY_EXECUTOR = SafeSympyExecutor(
    SafeSympyConfig(
        timeout_seconds=5.0,
        max_code_chars=64_000,
        max_output_chars=16_000,
        memory_limit_mb=768,
        cpu_limit_seconds=5,
    )
)


# ── Durable Stores + Task Runtime ──

STATE_DIR = WORKDIR / ".state"
STATE_DIR.mkdir(exist_ok=True)
LEGACY_TASKS_DIR = WORKDIR / ".tasks"
LEGACY_MEMORY_PATH = WORKDIR / ".memory" / "MEMORY.md"
TASK_LEASE_SECONDS = max(30, int(os.getenv("TASK_LEASE_SECONDS", "300")))
TASK_HEARTBEAT_INTERVAL = min(
    max(5, int(os.getenv("TASK_HEARTBEAT_INTERVAL", "60"))),
    max(5, TASK_LEASE_SECONDS // 2),
)

TASK_STORE = TaskStore(
    STATE_DIR / "tasks.db",
    default_lease_seconds=TASK_LEASE_SECONDS,
)
JOB_STORE = JobStore(STATE_DIR / "jobs.db")
MEMORY_STORE = MemoryStore(STATE_DIR / "memory.db")
CURRENT_TODOS: list[dict] = []

TASK_RUNTIME = TaskRuntime(
    TaskRuntimeConfig(legacy_tasks_dir=LEGACY_TASKS_DIR),
    TaskRuntimeDependencies(
        store=TASK_STORE,
        terminal_print=terminal_print,
        transition_sink=_trace_task_transition,
    ),
)
TASK_RUNTIME.initialize()

# Stable application-facing task contract used by worktree and teammate wiring.
create_task = TASK_RUNTIME.create_task
load_task = TASK_RUNTIME.load_task
list_tasks = TASK_RUNTIME.list_tasks
get_task_json = TASK_RUNTIME.get_task_json
can_start = TASK_RUNTIME.can_start
claim_task = TASK_RUNTIME.claim_task
heartbeat_task = TASK_RUNTIME.heartbeat_task
recover_expired_task_leases = TASK_RUNTIME.recover_expired_task_leases
recover_expired_tasks = TASK_RUNTIME.recover_expired_tasks_text
complete_task = TASK_RUNTIME.complete_task


# ── Worktree System ──

WORKTREES_DIR = WORKDIR / ".worktrees"
WORKTREES_DIR.mkdir(exist_ok=True)

WORKTREE_RUNTIME = WorktreeRuntime(
    WorktreeRuntimeConfig(
        workspace_root=WORKDIR,
        worktrees_dir=WORKTREES_DIR,
    ),
    WorktreeRuntimeDependencies(
        load_task=load_task,
        update_task_worktree=TASK_STORE.update_worktree,
        terminal_print=terminal_print,
    ),
)

validate_worktree_name = WORKTREE_RUNTIME.validate_name
run_git = WORKTREE_RUNTIME.run_git
log_event = WORKTREE_RUNTIME.log_event
create_worktree = WORKTREE_RUNTIME.create
bind_task_to_worktree = WORKTREE_RUNTIME.bind_task
_count_worktree_changes = WORKTREE_RUNTIME.count_changes
remove_worktree = WORKTREE_RUNTIME.remove
keep_worktree = WORKTREE_RUNTIME.keep


# ── Skill Loading ──

SKILL_CATALOG = SkillCatalog(SKILLS_DIR)
SKILL_REGISTRY = SKILL_CATALOG.registry
_parse_frontmatter = SKILL_CATALOG.parse_frontmatter
scan_skills = SKILL_CATALOG.scan
list_skills = SKILL_CATALOG.list_text
load_skill = SKILL_CATALOG.load


# ── Lead Agent Application ──
# Prompt/context assembly is configured near the runtime wiring section below.


# ── Basic Tool Execution ──

TOOL_EXECUTOR = ToolExecutionRuntime(
    ToolExecutionConfig(workspace=WORKDIR)
)
safe_path = TOOL_EXECUTOR.resolve_path
run_bash = TOOL_EXECUTOR.run_bash
run_read = TOOL_EXECUTOR.read_file
run_write = TOOL_EXECUTOR.write_file
run_edit = TOOL_EXECUTOR.edit_file
run_glob = TOOL_EXECUTOR.glob
call_tool_handler = TOOL_EXECUTOR.call_handler


# ── Memory Runtime ──

MEMORY_RUNTIME = MemoryRuntime(
    MemoryRuntimeConfig(
        workspace=WORKDIR,
        legacy_memory_path=LEGACY_MEMORY_PATH,
        context_max_chars=max(
            1000, int(os.getenv("MEMORY_CONTEXT_MAX_CHARS", "6000"))
        ),
    ),
    MemoryRuntimeDependencies(
        store=MEMORY_STORE,
        resolve_path=lambda path: safe_path(path),
        terminal_print=terminal_print,
    ),
)
MEMORY_RUNTIME.initialize_session()

# Tool-facing compatibility aliases. The CRUD/scope semantics now live in
# MemoryRuntime rather than the composition root.
run_memory_set_working = MEMORY_RUNTIME.set_working
run_memory_add_episode = MEMORY_RUNTIME.add_episode
run_memory_add_artifact = MEMORY_RUNTIME.add_artifact
run_list_memories = MEMORY_RUNTIME.list_memories
run_search_memories = MEMORY_RUNTIME.search_memories
run_memory_start_scope = MEMORY_RUNTIME.start_scope
run_memory_switch_scope = MEMORY_RUNTIME.switch_scope
run_memory_list_scopes = MEMORY_RUNTIME.list_scopes
run_memory_promote_global = MEMORY_RUNTIME.promote_global


def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    for i, todo in enumerate(todos):
        if "content" not in todo or "status" not in todo:
            return f"Error: todos[{i}] missing 'content' or 'status'"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return f"Error: todos[{i}] has invalid status '{todo['status']}'"
    CURRENT_TODOS = todos
    terminal_print(f"  \033[33m[todo] updated {len(CURRENT_TODOS)} item(s)\033[0m")
    return f"Updated {len(CURRENT_TODOS)} todos"


# ── Team Coordination ──

# Team communication is durable in SQLite. The legacy .mailboxes directory is
# imported once so upgrades do not silently lose unread JSONL messages. Protocol
# request state remains process-local in 10A; only MessageBus persistence changes.
MAILBOX_DIR = WORKDIR / ".mailboxes"
MESSAGE_STORE = MessageStore(STATE_DIR / "team.db")
TEAM_COORDINATOR = TeamCoordinator(
    mailbox_dir=MAILBOX_DIR,
    message_store=MESSAGE_STORE,
    terminal_print=terminal_print,
)
BUS = TEAM_COORDINATOR.bus


consume_lead_inbox = TEAM_COORDINATOR.consume_lead_inbox


# ── Autonomous Teammate Runtime ──

# The teammate execution loop now lives in agent_runtime.runtime.teammate.
# s20 wires concrete application/infrastructure dependencies and keeps thin
# compatibility functions for the tool registry and Lead application.
IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60
RESEARCH_TEAM_APPLICATION = ResearchTeamApplication()


def _create_teammate_message(*, system, messages, tools, max_tokens):
    return client.messages.create(
        model=MODEL,
        system=system,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
    )


TEAMMATE_RUNTIME = TeammateRuntime(
    TeammateRuntimeDependencies(
        application=RESEARCH_TEAM_APPLICATION,
        coordinator=TEAM_COORDINATOR,
        list_tasks=list_tasks,
        load_task=load_task,
        can_start=can_start,
        claim_task=claim_task,
        complete_task=complete_task,
        recover_expired_task_leases=recover_expired_task_leases,
        task_matches_worker=task_matches_worker,
        select_tasks_for_worker=select_tasks_for_worker,
        heartbeat_task=TASK_STORE.heartbeat_task,
        worktrees_dir=WORKTREES_DIR,
        run_bash=run_bash,
        run_read=run_read,
        run_write=run_write,
        call_tool_handler=call_tool_handler,
        # has_tool_use is defined later in the file; lookup is intentionally
        # deferred until the teammate actually receives a model response.
        has_tool_use=lambda content: has_tool_use(content),
        create_message=_create_teammate_message,
        terminal_print=terminal_print,
        verify_math_symbolic=SYMPY_EXECUTOR.execute,
        heartbeat_interval=TASK_HEARTBEAT_INTERVAL,
        idle_poll_interval=IDLE_POLL_INTERVAL,
        idle_timeout=IDLE_TIMEOUT,
    ),
    execution_tracker=EXECUTION_TRACKER,
)
active_teammates = TEAMMATE_RUNTIME.active_teammates


spawn_teammate_thread = TEAMMATE_RUNTIME.spawn

# ── Hard Mathematical Research Workflow ──

RESEARCH_WORKFLOW_COORDINATOR = ResearchWorkflowCoordinator()
RESEARCH_WORKFLOW_ORCHESTRATOR = ResearchWorkflowOrchestrator(
    RESEARCH_WORKFLOW_COORDINATOR,
    ResearchWorkflowOrchestratorDependencies(
        create_task=TASK_RUNTIME.create_task,
        load_task=TASK_RUNTIME.load_task,
        spawn_teammate=TEAMMATE_RUNTIME.spawn,
        send_message=TEAM_COORDINATOR.send_from_lead,
        create_worktree=create_worktree,
        bind_task_to_worktree=bind_task_to_worktree,
        terminal_print=terminal_print,
        record_event=_trace_workflow_event,
        save_snapshot=TRACE_STORE.save_workflow_snapshot,
        load_snapshots=TRACE_STORE.load_workflow_snapshots,
    ),
)
run_research_workflow = RESEARCH_WORKFLOW_ORCHESTRATOR.dispatch

# ── Lead Protocol Tools ──

run_request_shutdown = TEAM_COORDINATOR.request_shutdown
run_request_plan = TEAM_COORDINATOR.request_plan
run_review_plan = TEAM_COORDINATOR.review_plan


# ── Hooks + Permission Pipeline ──

# Runtime hook registration, policy evaluation, interactive approval, audit
# logging, and default observability hooks now live behind PolicyHookPipeline.
# The catalog-bound ToolRegistry is initialized later in this module, so
# capability lookup is injected lazily and resolved at tool-call time.
POLICY_PIPELINE = PolicyHookPipeline(
    WORKDIR,
    terminal_print=terminal_print,
    decision_sink=_trace_policy_decision,
    static_capabilities_for=lambda tool_name: (
        globals()["BASIC_TOOL_REGISTRY"].capabilities_for(tool_name)
        if "BASIC_TOOL_REGISTRY" in globals()
        else frozenset()
    ),
)

# Backward-compatible aliases consumed by Lead/Subagent/Teammate runtimes.
HOOKS = POLICY_PIPELINE.hooks
register_hook = POLICY_PIPELINE.register_hook
trigger_hooks = POLICY_PIPELINE.trigger_hooks


# ── Subagent Runtime ──

# The short-lived `task` subagent loop lives in runtime/subagent.py. Unlike
# autonomous teammates, subagents do not own durable tasks, inboxes, leases,
# worktrees, or plan-approval state; they execute one focused description and
# return a final summary to the caller.

def extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text").strip()


def has_tool_use(content):
    # Defensive normalization shared by Lead/Teammate runtime wiring.
    if content is None or not isinstance(content, list):
        return False

    return any(getattr(block, "type", None) == "tool_use" or
               (isinstance(block, dict) and block.get("type") == "tool_use")
               for block in content)


def _create_subagent_message(*, system, messages, tools, max_tokens):
    return client.messages.create(
        model=MODEL,
        system=system,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
    )


SUBAGENT_RUNTIME = SubagentRuntime(
    SubagentRuntimeConfig(
        workspace=WORKDIR,
        max_steps=30,
        max_tokens=8000,
    ),
    SubagentRuntimeDependencies(
        create_message=_create_subagent_message,
        trigger_hooks=trigger_hooks,
        call_tool_handler=call_tool_handler,
        run_bash=run_bash,
        run_read=run_read,
        run_write=run_write,
        run_edit=run_edit,
        run_glob=run_glob,
    ),
    execution_tracker=EXECUTION_TRACKER,
)


spawn_subagent = SUBAGENT_RUNTIME.run


# ── Context Compaction ──

# Context budgeting, transcript persistence, structured checkpoint extraction,
# and checkpoint-to-memory projection now live in agent_runtime/context/runtime.py.
# The concrete ContextRuntime is wired after the memory/context configuration
# section below, once all dependencies are available.


# ── Background Jobs ──

# Durable background tool execution now lives behind BackgroundJobRuntime.
# s20 keeps aliases only so existing Tool Registry and AgentRuntime wiring stay
# backward-compatible while the orchestration implementation is isolated.
BACKGROUND_JOBS = BackgroundJobRuntime(
    JOB_STORE,
    BackgroundJobRuntimeDependencies(
        call_tool_handler=call_tool_handler,
        trigger_hooks=trigger_hooks,
        terminal_print=terminal_print,
    ),
)
BACKGROUND_JOBS.recover_interrupted()

is_slow_operation = BACKGROUND_JOBS.is_slow_operation
should_run_background = BACKGROUND_JOBS.should_run
start_background_task = BACKGROUND_JOBS.start
resume_queued_background_jobs = BACKGROUND_JOBS.resume_queued
collect_background_results = BACKGROUND_JOBS.collect_notifications
run_list_background_jobs = BACKGROUND_JOBS.list_jobs_text
run_get_background_job = BACKGROUND_JOBS.get_job_text
build_user_content = BACKGROUND_JOBS.build_user_content
inject_background_notifications = BACKGROUND_JOBS.inject_notifications


# Memory tool handlers are provided by MEMORY_RUNTIME above.


# ── Cron Scheduler ──

# Schedule definitions, migration, polling, and scheduled-prompt delivery are
# coordinated by CronSchedulerRuntime over the durable JobStore.
LEGACY_CRON_PATH = WORKDIR / ".scheduled_tasks.json"
CRON_RUNNER_ID = f"main-agent-cron-{os.getpid()}"
CRON_SESSION_ID = f"cron-session-{os.getpid()}-{random.randint(0, 999999):06d}"

CRON_SCHEDULER = CronSchedulerRuntime(
    JOB_STORE,
    CronSchedulerConfig(
        legacy_path=LEGACY_CRON_PATH,
        runner_id=CRON_RUNNER_ID,
        session_id=CRON_SESSION_ID,
    ),
    CronSchedulerDependencies(terminal_print=terminal_print),
)
CRON_SCHEDULER.recover_interrupted()
CRON_SCHEDULER.migrate_legacy_file()
CRON_SCHEDULER.reconcile_one_shots()
CRON_SCHEDULER.start_scheduler_thread()

# Compatibility aliases used by existing tools and tests.
cron_matches = CRON_SCHEDULER.matches
validate_cron = CRON_SCHEDULER.validate
schedule_job = CRON_SCHEDULER.schedule


def cancel_job(job_id: str) -> str:
    # Keep the legacy keyword name because the tool schema sends ``job_id``.
    return CRON_SCHEDULER.cancel(job_id)


claim_cron_prompt_jobs = CRON_SCHEDULER.claim_prompt_jobs
run_schedule_cron = CRON_SCHEDULER.schedule_text
run_list_crons = CRON_SCHEDULER.list_text
run_cancel_cron = cancel_job


# ── Research / MCP Integrations ──

# Research-domain network/file helpers are separated from the generic Tool
# Execution Runtime. Real MCP stdio and Streamable HTTP servers are declared in
# a trusted config outside the agent-writable workspace. Persistent MCP client
# contexts are owned by the MCP runtime; the old docs/deploy teaching servers
# remain an explicit fallback for local demos.
RESEARCH_TOOLS = ResearchToolRuntime(
    base_dir=Path(__file__).parent.resolve(),
    terminal_print=terminal_print,
)
MCP_CONFIG_PATH = Path(
    os.getenv(
        "MCP_CONFIG_PATH",
        str(Path.home() / ".researchagent" / "mcp_servers.json"),
    )
).expanduser()
MCP_RUNTIME = MCPRuntime(
    MCP_CONFIG_PATH,
    workspace=WORKDIR,
    terminal_print=terminal_print,
)
# Bound only the optional external literature backend. Other MCP services keep
# their configured lifecycle policy.
if "docs" in MCP_RUNTIME.specs:
    docs_spec = MCP_RUNTIME.specs["docs"]
    MCP_RUNTIME.specs["docs"] = replace(
        docs_spec,
        timeout_seconds=min(docs_spec.timeout_seconds, 5.0),
        reconnect_attempts=min(docs_spec.reconnect_attempts, 1),
        shutdown_timeout_seconds=min(docs_spec.shutdown_timeout_seconds, 1.0),
    )
mcp_clients = MCP_RUNTIME.clients
normalize_mcp_name = MCP_RUNTIME.normalize_name
connect_mcp = MCP_RUNTIME.connect


def assemble_tool_pool() -> tuple[list[dict], dict]:
    """Merge registry tools + control tools + dynamically discovered MCP tools."""
    return MCP_RUNTIME.assemble_tool_pool(
        BASIC_TOOL_REGISTRY.schemas(),
        list(BUILTIN_TOOLS),
        BASIC_TOOL_REGISTRY.handlers(),
    )


# ── Lead Worktree Tools ──

run_create_worktree = create_worktree
run_remove_worktree = remove_worktree
run_keep_worktree = keep_worktree


# ── Basic tool handlers ──

run_create_task = TASK_RUNTIME.create_task_text
run_list_tasks = TASK_RUNTIME.list_tasks_text
run_get_task = TASK_RUNTIME.get_task_text
run_claim_task = TASK_RUNTIME.claim_task_text
run_complete_task = TASK_RUNTIME.complete_task_text
run_heartbeat_task = TASK_RUNTIME.heartbeat_task_text
run_recover_expired_tasks = TASK_RUNTIME.recover_expired_tasks_text

run_spawn_teammate = spawn_teammate_thread
run_send_message = TEAM_COORDINATOR.send_from_lead
run_check_inbox = TEAM_COORDINATOR.format_lead_inbox
run_connect_mcp = connect_mcp
run_search_literature = RESEARCH_TOOLS.search_literature
run_download_arxiv_source = RESEARCH_TOOLS.download_arxiv_source
run_view_latex_theorem = RESEARCH_TOOLS.view_latex_theorem


# ── Tool Catalog Binding ──

# Static schemas/capabilities live in agent_runtime.tools.catalog.  This module
# only binds those declarations to the concrete runtime handlers assembled here.
BASIC_TOOL_HANDLERS = {
    'bash': run_bash,
    'read_file': run_read,
    'write_file': run_write,
    'edit_file': run_edit,
    'glob': run_glob,
    'search_literature': run_search_literature,
    'download_arxiv_source': run_download_arxiv_source,
    'view_latex_theorem': run_view_latex_theorem,
    'todo_write': run_todo_write,
    'task': spawn_subagent,
    'load_skill': load_skill,
    'create_task': run_create_task,
    'list_tasks': run_list_tasks,
    'get_task': run_get_task,
    'claim_task': run_claim_task,
    'complete_task': run_complete_task,
    'heartbeat_task': run_heartbeat_task,
    'recover_expired_tasks': run_recover_expired_tasks,
    'schedule_cron': run_schedule_cron,
    'list_crons': run_list_crons,
    'cancel_cron': run_cancel_cron,
    'spawn_teammate': run_spawn_teammate,
    'send_message': run_send_message,
    'check_inbox': run_check_inbox,
    'request_shutdown': run_request_shutdown,
    'request_plan': run_request_plan,
    'review_plan': run_review_plan,
    'create_worktree': run_create_worktree,
    'remove_worktree': run_remove_worktree,
    'keep_worktree': run_keep_worktree,
    'connect_mcp': run_connect_mcp,
    'research_workflow': run_research_workflow,
    'list_background_jobs': run_list_background_jobs,
    'get_background_job': run_get_background_job,
    'memory_set_working': run_memory_set_working,
    'memory_add_episode': run_memory_add_episode,
    'memory_add_artifact': run_memory_add_artifact,
    'list_memories': run_list_memories,
    'search_memories': run_search_memories,
    'memory_start_scope': run_memory_start_scope,
    'memory_switch_scope': run_memory_switch_scope,
    'memory_list_scopes': run_memory_list_scopes,
    'memory_promote_global': run_memory_promote_global,
}

BASIC_TOOL_REGISTRY = build_basic_tool_registry(BASIC_TOOL_HANDLERS)

BUILTIN_TOOLS = [
    {"name": "compact",
     "description": "Summarize earlier conversation and continue with compacted context.",
     "input_schema": {"type": "object",
                      "properties": {"focus": {"type": "string"}},
                      "required": []}},
]


# ── Context ──

CONTEXT_TOKEN_LIMIT = max(
    1000,
    int(os.getenv("CONTEXT_TOKEN_LIMIT", str(max(2000, CONTEXT_LIMIT // 4)))),
)
def _summarize_context_checkpoint(prompt: str) -> str:
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2500,
    )
    return extract_text(response.content) or ""


CONTEXT_RUNTIME = ContextRuntime(
    ContextRuntimeConfig(
        transcript_dir=TRANSCRIPT_DIR,
        tool_results_dir=TOOL_RESULTS_DIR,
        persist_threshold=PERSIST_THRESHOLD,
        token_limit=CONTEXT_TOKEN_LIMIT,
        soft_limit_ratio=0.85,
        keep_recent_tool_results=KEEP_RECENT_TOOL_RESULTS,
        tool_result_compact_threshold_tokens=120,
        tool_result_max_bytes=200_000,
    ),
    ContextRuntimeDependencies(
        summarize_checkpoint=_summarize_context_checkpoint,
        memory_store=MEMORY_STORE,
        get_active_scope_id=lambda: MEMORY_RUNTIME.active_scope_id,
        terminal_print=terminal_print,
    ),
)

# Backward-compatible aliases consumed by AgentRuntime wiring.
persist_large_output = CONTEXT_RUNTIME.persist_large_output
tool_result_budget = CONTEXT_RUNTIME.tool_result_budget
write_transcript = CONTEXT_RUNTIME.write_transcript
compact_history = CONTEXT_RUNTIME.compact_history
reactive_compact = CONTEXT_RUNTIME.reactive_compact
prepare_context = CONTEXT_RUNTIME.prepare_context


LEAD_AGENT_APPLICATION = LeadAgentApplication(
    LeadAgentDependencies(
        workspace=WORKDIR,
        list_skills=list_skills,
        build_memory_context=MEMORY_RUNTIME.build_context,
        get_active_memory_scope=lambda: MEMORY_RUNTIME.active_scope_id,
        list_mcp_servers=MCP_RUNTIME.list_servers,
        list_active_teammates=lambda: list(active_teammates.keys()),
    )
)


assemble_system_prompt = LEAD_AGENT_APPLICATION.build_system_prompt

def update_context(context: dict, messages: list) -> dict:
    # Preserve the historical two-argument callback contract used by runtimes.
    return LEAD_AGENT_APPLICATION.build_context(messages)


# ── Agent Loop ──

MODEL_GATEWAY = AnthropicModelGateway(
    client=client,
    prompt_builder=assemble_system_prompt,
    config=AnthropicGatewayConfig(
        primary_model=PRIMARY_MODEL,
        fallback_model=FALLBACK_MODEL,
        max_retries=MAX_RETRIES,
        max_consecutive_529=MAX_CONSECUTIVE_529,
        base_delay_ms=BASE_DELAY_MS,
    ),
)


AGENT_RUNTIME = AgentRuntime(
    AgentRuntimeConfig(
        default_max_tokens=DEFAULT_MAX_TOKENS,
        escalated_max_tokens=ESCALATED_MAX_TOKENS,
        max_recovery_retries=MAX_RECOVERY_RETRIES,
        continuation_prompt=CONTINUATION_PROMPT,
        todo_reminder_interval=3,
    ),
    AgentRuntimeDependencies(
        assemble_tool_pool=assemble_tool_pool,
        resume_queued_background_jobs=resume_queued_background_jobs,
        claim_scheduled_prompts=claim_cron_prompt_jobs,
        mark_scheduled_prompt_delivered=CRON_SCHEDULER.mark_prompt_delivered,
        inject_background_notifications=inject_background_notifications,
        prepare_context=prepare_context,
        update_context=update_context,
        call_llm=MODEL_GATEWAY.call,
        make_recovery_state=MODEL_GATEWAY.new_recovery_state,
        is_prompt_too_long_error=MODEL_GATEWAY.is_prompt_too_long_error,
        reactive_compact=reactive_compact,
        has_tool_use=has_tool_use,
        trigger_hooks=trigger_hooks,
        compact_history=compact_history,
        should_run_background=should_run_background,
        start_background_task=start_background_task,
        call_tool_handler=call_tool_handler,
        build_user_content=build_user_content,
        research_run_stage=lambda run_id: (
            RESEARCH_WORKFLOW_ORCHESTRATOR.runs[run_id].stage.value
            if run_id in RESEARCH_WORKFLOW_ORCHESTRATOR.runs else None
        ),
        record_literature_tool_result=RESEARCH_WORKFLOW_ORCHESTRATOR.record_literature_tool_result,
        record_literature_query_started=RESEARCH_WORKFLOW_ORCHESTRATOR.record_literature_query_started,
    ),
    execution_tracker=EXECUTION_TRACKER,
)


agent_loop = AGENT_RUNTIME.run

CLI_APPLICATION = AgentCLIApplication(
    AgentCLIApplicationDependencies(
        renderer=CLI_RENDERER,
        terminal_print=terminal_print,
        trigger_hooks=trigger_hooks,
        update_context=update_context,
        agent_loop=agent_loop,
        consume_lead_inbox=consume_lead_inbox,
        cron_scheduler=CRON_SCHEDULER,
    )
)


def main() -> None:
    try:
        CLI_APPLICATION.run()
    finally:
        MCP_RUNTIME.close()


if __name__ == "__main__":
    main()
