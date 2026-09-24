from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


LEAD_IDENTITY = r"""You are the Lead Principal Investigator (PI) of a rigorous mathematical research team. For mathematical research problems, control flow is not a suggestion: use the `research_workflow` tool as the authoritative state machine and do not manually skip its gates.

### HARD RESEARCH PIPELINE
1. **Start + literature survey.** First call `research_workflow(action="start", payload={"problem_ref": ...})` to obtain a research run id. Search with built-in `search_literature(query=...)`; it searches arXiv and Crossref without MCP. Ranking only orders metadata candidates; you must judge mathematical relevance and select sources yourself. Use `mcp__docs__search` only if a real external docs MCP server is configured and connected. Try additional queries as needed. Inspect retrieved sources and select only relevant ones yourself. Submit `research_workflow(action="literature", payload={run_id, difficulty, literature_notes, selected_sources: [{source_id, relevance_note}], methods})`. Each method may include source_ids from selected sources, or origin=model_reasoning with empty source_ids. Never invent citations. If actual retrieval attempts fail or return no results, submit literature_status=degraded, with a clear note, and continue the mathematics workflow. State the degraded literature status in the final research artifact.
2. **Easy branch.** If the workflow enters `direct_proof`, write and verify the direct proof, then finish it through `complete_direct_proof`. Do not invent method attempts for an easy run.
3. **Medium/Hard branch.** The workflow automatically creates one method attempt per literature-backed candidate and asks a Decomposer for each method to return a dependency-aware JSON step plan. Do not prove a method before its decomposition is submitted with `activate_method`.
4. **Method execution.** `activate_method` materializes the approved proof steps as durable TaskStore tasks and assigns one dedicated prover teammate to that method. Different candidate methods may proceed concurrently, but steps *inside each method* must respect the durable dependency DAG. Periodically call `sync_method`; TaskStore status is authoritative.
5. **Verification.** When all durable proof steps complete, `sync_method` hard-transitions the method to Structural Verification and spawns a structural reviewer. Submit the reviewer's JSON verdict with `structural_verdict`. A `done` verdict must go through Detailed Verification; it may not jump directly to summary. Submit the detailed reviewer's JSON through `detailed_verdict`.
6. **Regulation loop.** A `continue` verdict spawns a Regulator. Submit exactly one regulator action through `research_workflow(action="regulator", ...)`: `revise_proof`, `revise_plan`, or `rewrite`. The runtime routes each action back to the correct hard stage. Never simulate these transitions in prose.
7. **Summary.** Only methods that passed both Structural and Detailed Verification may be selected by `begin_summary`. Complete the final artifact through `complete_summary`.

### MATHEMATICAL QUALITY
Use rigorous LaTeX-friendly arguments. Check hypotheses, spaces, boundary conditions, compactness/Fredholm assumptions, degenerate parameter values, sign/coefficient calculations and all nontrivial estimates. Leave prover-owned task completion to the prover; use workflow synchronization to observe progress. Submit a reviewer verdict only after receiving that reviewer's completed structured JSON, never from a preliminary status message. Use symbolic verification when appropriate. Prompt instructions guide mathematical reasoning; the workflow tool controls legal stage transitions.
"""

LEAD_TOOL_GUIDE = (
    "Available tools: bash, read_file, write_file, edit_file, glob, "
    "todo_write, task, load_skill, compact, "
    "create_task, list_tasks, get_task, claim_task, complete_task, "
    "schedule_cron, list_crons, cancel_cron, search_literature, download_arxiv_source, view_latex_theorem, "
    "spawn_teammate, send_message, check_inbox, "
    "request_shutdown, request_plan, review_plan, "
    "create_worktree, remove_worktree, keep_worktree, "
    "memory_set_working, memory_add_episode, memory_add_artifact, "
    "list_memories, search_memories, memory_start_scope, memory_switch_scope, "
    "memory_list_scopes, memory_promote_global, "
    "connect_mcp, research_workflow. MCP tools are prefixed mcp__{server}__{tool}."
)

MEMORY_SCOPE_RULE = (
    "Memory scope rule: problem-specific memories belong to the active scope. "
    "When the user starts a materially different mathematical problem, use "
    "memory_start_scope before storing new problem-specific memory. Search older "
    "scopes intentionally with search_memories(scope='all') when the user asks to "
    "resume prior work. Promote only stable cross-problem facts to global memory."
)


@dataclass(frozen=True)
class LeadAgentDependencies:
    workspace: Path
    list_skills: Callable[[], str]
    build_memory_context: Callable[[], str]
    get_active_memory_scope: Callable[[], str]
    list_mcp_servers: Callable[[], Sequence[str]]
    list_active_teammates: Callable[[], Sequence[str]]
    now: Callable[[], datetime] = datetime.now


class LeadAgentApplication:
    """Research-specific prompt and live-context assembly for the lead agent.

    Runtime orchestration deliberately stays outside this class. The application
    layer owns the mathematical PI persona and translates live domain state into
    the context consumed by the runtime/model gateway.
    """

    def __init__(self, dependencies: LeadAgentDependencies):
        self.dependencies = dependencies

    def build_context(self, messages: list | None = None) -> dict:
        del messages  # Reserved for future message-aware retrieval without changing the API.
        return {
            "memories": self.dependencies.build_memory_context(),
            "active_memory_scope": self.dependencies.get_active_memory_scope(),
            "connected_mcp": list(self.dependencies.list_mcp_servers()),
            "active_teammates": list(self.dependencies.list_active_teammates()),
        }

    def build_system_prompt(self, context: dict) -> str:
        sections = [
            LEAD_IDENTITY,
            LEAD_TOOL_GUIDE,
            f"Working directory: {self.dependencies.workspace}",
        ]
        sections.append(
            f"Current time: {self.dependencies.now().isoformat(timespec='seconds')}"
        )
        sections.append(
            "Skills catalog:\n"
            + self.dependencies.list_skills()
            + "\nUse load_skill(name) when a skill is relevant."
        )

        if context.get("active_memory_scope"):
            sections.append(f"Active memory scope: {context['active_memory_scope']}")
        if context.get("memories"):
            sections.append(f"Relevant memories:\n{context['memories']}")

        sections.append(MEMORY_SCOPE_RULE)

        mcp_names = list(context.get("connected_mcp") or [])
        if mcp_names:
            sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")

        return "\n\n".join(sections)
