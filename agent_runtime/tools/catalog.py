from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .models import ToolDefinition
from .registry import ToolRegistry


@dataclass(frozen=True)
class ToolCatalogEntry:
    """Static model-facing metadata for one builtin tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))

    def bind(self, handler: Callable[..., str]) -> ToolDefinition:
        """Create an executable ToolDefinition without mutating catalog metadata."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=deepcopy(self.input_schema),
            capabilities=self.capabilities,
            handler=handler,
        )


BASIC_TOOL_CATALOG: tuple[ToolCatalogEntry, ...] = (
    ToolCatalogEntry(
        name='bash',
        description=('Run one allowlisted command without a host shell. Shell operators '
                     '(such as &&, |, >), shell interpreters, and inline Python are rejected; '
                     'use separate tool calls for separate processes.'),
        input_schema={'type': 'object',
         'properties': {'command': {'type': 'string'}, 'run_in_background': {'type': 'boolean'}},
         'required': ['command']},
        capabilities=frozenset({'shell.execute'}),
    ),
    ToolCatalogEntry(
        name='read_file',
        description='Read file contents.',
        input_schema={'type': 'object',
         'properties': {'path': {'type': 'string'},
                        'limit': {'type': 'integer'},
                        'offset': {'type': 'integer'}},
         'required': ['path']},
        capabilities=frozenset({'filesystem.read'}),
    ),
    ToolCatalogEntry(
        name='write_file',
        description='Write content to a file.',
        input_schema={'type': 'object',
         'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
         'required': ['path', 'content']},
        capabilities=frozenset({'filesystem.write'}),
    ),
    ToolCatalogEntry(
        name='edit_file',
        description='Replace exact text in a file once.',
        input_schema={'type': 'object',
         'properties': {'path': {'type': 'string'},
                        'old_text': {'type': 'string'},
                        'new_text': {'type': 'string'}},
         'required': ['path', 'old_text', 'new_text']},
        capabilities=frozenset({'filesystem.read', 'filesystem.write'}),
    ),
    ToolCatalogEntry(
        name='glob',
        description='Find files matching a glob pattern.',
        input_schema={'type': 'object', 'properties': {'pattern': {'type': 'string'}}, 'required': ['pattern']},
        capabilities=frozenset({'filesystem.read'}),
    ),
    ToolCatalogEntry(
        name='search_literature',
        description='Search arXiv and Crossref with built-in literature providers. Returns ranked metadata candidates and per-provider status; the Lead decides relevance and selection. No MCP connection is required. (readOnly)',
        input_schema={'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']},
        capabilities=frozenset({'network.access'}),
    ),
    ToolCatalogEntry(
        name='download_arxiv_source',
        description="Download and extract the full LaTeX source code of a paper from arXiv by its Arxiv_ID into the 'Conference' directory.",
        input_schema={'type': 'object',
         'properties': {'arxiv_id': {'type': 'string',
                                     'description': "The arXiv ID, e.g., '2203.xxxxx'"}},
         'required': ['arxiv_id']},
        capabilities=frozenset({'filesystem.write', 'network.access'}),
    ),
    ToolCatalogEntry(
        name='view_latex_theorem',
        description='Locate and view a specific Theorem, Lemma, or a targeted range of lines from the LaTeX file without loading the whole document.',
        input_schema={'type': 'object',
         'properties': {'file_path': {'type': 'string',
                                      'description': 'Path to the .tex file, e.g., '
                                                     "'Conference/.../main.tex'"},
                        'start_line': {'type': 'integer',
                                       'description': 'The line number where the theorem/proof '
                                                      'starts'},
                        'num_lines': {'type': 'integer',
                                      'description': 'How many lines to read from the start_line '
                                                     '(default: 150)'}},
         'required': ['file_path', 'start_line']},
        capabilities=frozenset({'filesystem.read'}),
    ),
    ToolCatalogEntry(
        name='todo_write',
        description='Create and manage a task list for the current session.',
        input_schema={'type': 'object',
         'properties': {'todos': {'type': 'array',
                                  'items': {'type': 'object',
                                            'properties': {'content': {'type': 'string'},
                                                           'status': {'type': 'string',
                                                                      'enum': ['pending',
                                                                               'in_progress',
                                                                               'completed']}},
                                            'required': ['content', 'status']}}},
         'required': ['todos']},
        capabilities=frozenset({'session.write'}),
    ),
    ToolCatalogEntry(
        name='task',
        description='Launch a focused subagent. Returns only its final summary.',
        input_schema={'type': 'object',
         'properties': {'description': {'type': 'string'}},
         'required': ['description']},
        capabilities=frozenset({'agent.spawn'}),
    ),
    ToolCatalogEntry(
        name='load_skill',
        description='Load the full content of a skill by name.',
        input_schema={'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']},
        capabilities=frozenset({'skill.read'}),
    ),
    ToolCatalogEntry(
        name='create_task',
        description='Create a task.',
        input_schema={'type': 'object',
         'properties': {'subject': {'type': 'string'},
                        'description': {'type': 'string'},
                        'blockedBy': {'type': 'array', 'items': {'type': 'string'}},
                        'task_type': {'type': 'string',
                                      'enum': ['general', 'math', 'engineering', 'literature'],
                                      'description': 'Affinity category used by autonomous '
                                                     'teammate selection.'},
                        'required_roles': {'type': 'array',
                                           'items': {'type': 'string'},
                                           'description': 'Explicit worker roles allowed to '
                                                          'auto-claim this task.'},
                        'tags': {'type': 'array', 'items': {'type': 'string'}}},
         'required': ['subject']},
        capabilities=frozenset({'task.write'}),
    ),
    ToolCatalogEntry(
        name='list_tasks',
        description='List all tasks.',
        input_schema={'type': 'object', 'properties': {}, 'required': []},
        capabilities=frozenset({'task.read'}),
    ),
    ToolCatalogEntry(
        name='get_task',
        description='Get full task details.',
        input_schema={'type': 'object', 'properties': {'task_id': {'type': 'string'}}, 'required': ['task_id']},
        capabilities=frozenset({'task.read'}),
    ),
    ToolCatalogEntry(
        name='claim_task',
        description='Claim a pending task.',
        input_schema={'type': 'object', 'properties': {'task_id': {'type': 'string'}}, 'required': ['task_id']},
        capabilities=frozenset({'task.write'}),
    ),
    ToolCatalogEntry(
        name='complete_task',
        description='Complete an in-progress task.',
        input_schema={'type': 'object', 'properties': {'task_id': {'type': 'string'}}, 'required': ['task_id']},
        capabilities=frozenset({'task.write'}),
    ),
    ToolCatalogEntry(
        name='heartbeat_task',
        description="Renew the current agent's lease on an in-progress task.",
        input_schema={'type': 'object', 'properties': {'task_id': {'type': 'string'}}, 'required': ['task_id']},
        capabilities=frozenset({'task.write'}),
    ),
    ToolCatalogEntry(
        name='recover_expired_tasks',
        description='Recover tasks whose worker leases have expired.',
        input_schema={'type': 'object', 'properties': {}, 'required': []},
        capabilities=frozenset({'task.write'}),
    ),
    ToolCatalogEntry(
        name='schedule_cron',
        description='Schedule a cron job. cron is 5-field: min hour dom month dow. For one-shot reminders, compute the target minute and set recurring=false.',
        input_schema={'type': 'object',
         'properties': {'cron': {'type': 'string'},
                        'prompt': {'type': 'string'},
                        'recurring': {'type': 'boolean'},
                        'durable': {'type': 'boolean'}},
         'required': ['cron', 'prompt']},
        capabilities=frozenset({'scheduler.write'}),
    ),
    ToolCatalogEntry(
        name='list_crons',
        description='List registered cron jobs.',
        input_schema={'type': 'object', 'properties': {}, 'required': []},
        capabilities=frozenset({'scheduler.read'}),
    ),
    ToolCatalogEntry(
        name='cancel_cron',
        description='Cancel a cron job by ID.',
        input_schema={'type': 'object', 'properties': {'job_id': {'type': 'string'}}, 'required': ['job_id']},
        capabilities=frozenset({'scheduler.write'}),
    ),
    ToolCatalogEntry(
        name='spawn_teammate',
        description='Spawn an autonomous teammate.',
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string'},
                        'role': {'type': 'string'},
                        'prompt': {'type': 'string'}},
         'required': ['name', 'role', 'prompt']},
        capabilities=frozenset({'agent.spawn'}),
    ),
    ToolCatalogEntry(
        name='send_message',
        description='Send message to a teammate.',
        input_schema={'type': 'object',
         'properties': {'to': {'type': 'string'}, 'content': {'type': 'string'}},
         'required': ['to', 'content']},
        capabilities=frozenset({'agent.message'}),
    ),
    ToolCatalogEntry(
        name='check_inbox',
        description='Check inbox for messages and protocol responses.',
        input_schema={'type': 'object', 'properties': {}, 'required': []},
        capabilities=frozenset({'agent.message'}),
    ),
    ToolCatalogEntry(
        name='request_shutdown',
        description='Request a teammate to shut down.',
        input_schema={'type': 'object', 'properties': {'teammate': {'type': 'string'}}, 'required': ['teammate']},
        capabilities=frozenset({'agent.control'}),
    ),
    ToolCatalogEntry(
        name='request_plan',
        description='Ask a teammate to submit a plan.',
        input_schema={'type': 'object',
         'properties': {'teammate': {'type': 'string'}, 'task': {'type': 'string'}},
         'required': ['teammate', 'task']},
        capabilities=frozenset({'agent.control'}),
    ),
    ToolCatalogEntry(
        name='review_plan',
        description='Approve or reject a submitted plan.',
        input_schema={'type': 'object',
         'properties': {'request_id': {'type': 'string'},
                        'approve': {'type': 'boolean'},
                        'feedback': {'type': 'string'}},
         'required': ['request_id', 'approve']},
        capabilities=frozenset({'agent.control'}),
    ),
    ToolCatalogEntry(
        name='create_worktree',
        description='Create an isolated git worktree.',
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string'}, 'task_id': {'type': 'string'}},
         'required': ['name']},
        capabilities=frozenset({'filesystem.write', 'vcs.write'}),
    ),
    ToolCatalogEntry(
        name='remove_worktree',
        description='Remove a worktree. Refuses if changes exist.',
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string'}, 'discard_changes': {'type': 'boolean'}},
         'required': ['name']},
        capabilities=frozenset({'filesystem.write', 'vcs.write'}),
    ),
    ToolCatalogEntry(
        name='keep_worktree',
        description='Keep a worktree for manual review.',
        input_schema={'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']},
        capabilities=frozenset({'vcs.write'}),
    ),
    ToolCatalogEntry(
        name='connect_mcp',
        description='Connect or reconnect to a configured stdio or Streamable HTTP MCP server by name and discover its tools.',
        input_schema={'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']},
        capabilities=frozenset({'mcp.connect', 'network.access'}),
    ),
    ToolCatalogEntry(
        name='research_workflow',
        description=(
            'Hard control-plane for the mathematical research workflow. Use action=start, literature, '
            'request_decomposition, activate_method, sync_method, structural_verdict, detailed_verdict, '
            'regulator, begin_summary, complete_summary, complete_direct_proof, or status. The payload '
            'object carries action-specific structured data; illegal stage transitions are rejected.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'enum': [
                        'start', 'literature', 'request_decomposition', 'activate_method',
                        'sync_method', 'structural_verdict', 'detailed_verdict', 'regulator',
                        'begin_summary', 'complete_summary', 'complete_direct_proof', 'status'
                    ]
                },
                'payload': {
                    'type': 'object',
                    'description': 'Action-specific structured workflow payload. For literature, include run_id, difficulty, literature_notes, selected_sources with source_id and relevance_note, and optionally literature_status=degraded. The runtime fills run_id only when exactly one active run is unambiguous.',
                    'additionalProperties': True,
                },
            },
            'required': ['action'],
        },
        capabilities=frozenset({'research.workflow.write', 'task.write', 'agent.spawn'}),
    ),
    ToolCatalogEntry(
        name='list_background_jobs',
        description='List durable background tool jobs and their current status.',
        input_schema={'type': 'object',
         'properties': {'status': {'type': 'string', 'description': 'Optional job status filter.'}}},
        capabilities=frozenset({'job.read'}),
    ),
    ToolCatalogEntry(
        name='get_background_job',
        description='Inspect one durable background tool job by id.',
        input_schema={'type': 'object', 'properties': {'job_id': {'type': 'string'}}, 'required': ['job_id']},
        capabilities=frozenset({'job.read'}),
    ),
    ToolCatalogEntry(
        name='memory_set_working',
        description='Upsert durable working memory by an exact key.',
        input_schema={'type': 'object',
         'properties': {'key': {'type': 'string'},
                        'content': {'type': 'string'},
                        'priority': {'type': 'integer', 'minimum': 0, 'maximum': 100}},
         'required': ['key', 'content']},
        capabilities=frozenset({'memory.write'}),
    ),
    ToolCatalogEntry(
        name='memory_add_episode',
        description='Append a durable episodic memory such as a completed proof step or decision.',
        input_schema={'type': 'object',
         'properties': {'content': {'type': 'string'},
                        'source': {'type': 'string'},
                        'priority': {'type': 'integer', 'minimum': 0, 'maximum': 100}},
         'required': ['content']},
        capabilities=frozenset({'memory.write'}),
    ),
    ToolCatalogEntry(
        name='memory_add_artifact',
        description='Remember a workspace artifact by exact path and description.',
        input_schema={'type': 'object',
         'properties': {'path': {'type': 'string'},
                        'description': {'type': 'string'},
                        'artifact_type': {'type': 'string'},
                        'priority': {'type': 'integer', 'minimum': 0, 'maximum': 100}},
         'required': ['path']},
        capabilities=frozenset({'filesystem.read', 'memory.write'}),
    ),
    ToolCatalogEntry(
        name='list_memories',
        description='List deterministic structured memories by kind and priority.',
        input_schema={'type': 'object',
         'properties': {'kind': {'type': 'string', 'enum': ['working', 'episodic', 'artifact']},
                        'scope': {'type': 'string', 'enum': ['current', 'global', 'all']},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}},
        capabilities=frozenset({'memory.read'}),
    ),
    ToolCatalogEntry(
        name='search_memories',
        description='Search structured memory using deterministic case-insensitive keyword matching; no embeddings are used.',
        input_schema={'type': 'object',
         'properties': {'query': {'type': 'string'},
                        'kind': {'type': 'string', 'enum': ['working', 'episodic', 'artifact']},
                        'scope': {'type': 'string', 'enum': ['current', 'global', 'all']},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}},
         'required': ['query']},
        capabilities=frozenset({'memory.read'}),
    ),
    ToolCatalogEntry(
        name='memory_start_scope',
        description='Start a fresh problem-specific memory scope and archive the previous active scope. Use this when the user begins a materially different mathematical problem.',
        input_schema={'type': 'object', 'properties': {'label': {'type': 'string'}}, 'required': ['label']},
        capabilities=frozenset({'memory.write'}),
    ),
    ToolCatalogEntry(
        name='memory_switch_scope',
        description='Switch back to a previously created problem memory scope by exact scope id.',
        input_schema={'type': 'object', 'properties': {'scope_id': {'type': 'string'}}, 'required': ['scope_id']},
        capabilities=frozenset({'memory.write'}),
    ),
    ToolCatalogEntry(
        name='memory_list_scopes',
        description='List memory scopes so prior mathematical problems can be resumed intentionally.',
        input_schema={'type': 'object',
         'properties': {'include_archived': {'type': 'boolean'},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500}}},
        capabilities=frozenset({'memory.read'}),
    ),
    ToolCatalogEntry(
        name='memory_promote_global',
        description='Copy one memory into global memory so it is injected across problem scopes. Use only for stable cross-problem facts, not a problem-specific proof strategy.',
        input_schema={'type': 'object',
         'properties': {'memory_id': {'type': 'string'}},
         'required': ['memory_id']},
        capabilities=frozenset({'memory.write'}),
    ),
)


def catalog_names() -> tuple[str, ...]:
    return tuple(entry.name for entry in BASIC_TOOL_CATALOG)


def build_basic_tool_registry(
    handlers: Mapping[str, Callable[..., str]],
) -> ToolRegistry:
    """Bind static catalog metadata to runtime handlers in catalog order."""
    names = catalog_names()
    if len(names) != len(set(names)):
        raise ValueError("Duplicate tool names in BASIC_TOOL_CATALOG")

    missing = [name for name in names if name not in handlers]
    if missing:
        raise KeyError(
            "Missing builtin tool handler(s): " + ", ".join(missing)
        )

    registry = ToolRegistry()
    for entry in BASIC_TOOL_CATALOG:
        handler = handlers[entry.name]
        if not callable(handler):
            raise TypeError(f"Handler for tool '{entry.name}' is not callable")
        registry.register(entry.bind(handler))
    return registry
