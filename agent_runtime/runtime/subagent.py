from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable

from .execution import (
    AgentKind,
    ExecutionStatus,
    ExecutionTracker,
    RuntimeEventType,
    ToolCallStatus,
    response_usage_payload,
)


SUBAGENT_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": "Find files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
]


@dataclass(frozen=True)
class SubagentRuntimeConfig:
    """Configuration for a short-lived focused subagent.

    A SubagentRuntime is intentionally different from TeammateRuntime: it does
    not own durable tasks, poll an inbox, heartbeat a lease, or wait on plan
    approval. It executes one delegated description and returns a final text
    summary to the caller.
    """

    workspace: Path
    max_steps: int = 30
    max_tokens: int = 8000

    @property
    def system_prompt(self) -> str:
        return (
            f"You are a coding subagent at {self.workspace}. "
            "Complete the task, then return a concise final summary. "
            "Do not spawn more agents."
        )


@dataclass(frozen=True)
class SubagentRuntimeDependencies:
    create_message: Callable[..., Any]
    trigger_hooks: Callable[..., Any]
    call_tool_handler: Callable[[Any, dict, str], Any]
    run_bash: Callable[..., Any]
    run_read: Callable[..., Any]
    run_write: Callable[..., Any]
    run_edit: Callable[..., Any]
    run_glob: Callable[..., Any]


class SubagentRuntime:
    """Runs an ephemeral delegated task using a restricted local tool set."""

    def __init__(
        self,
        config: SubagentRuntimeConfig,
        deps: SubagentRuntimeDependencies,
        *,
        execution_tracker: ExecutionTracker | None = None,
    ):
        self.config = config
        self.deps = deps
        self.execution_tracker = execution_tracker
        self.handlers = {
            "bash": deps.run_bash,
            "read_file": deps.run_read,
            "write_file": deps.run_write,
            "edit_file": deps.run_edit,
            "glob": deps.run_glob,
        }

    @staticmethod
    def _has_tool_use(content: Any) -> bool:
        if content is None or not isinstance(content, list):
            return False
        return any(
            getattr(block, "type", None) == "tool_use"
            or (isinstance(block, dict) and block.get("type") == "tool_use")
            for block in content
        )

    @staticmethod
    def _extract_text(content: Any) -> str:
        if not isinstance(content, list):
            return str(content)
        return "\n".join(
            getattr(block, "text", "")
            for block in content
            if getattr(block, "type", None) == "text"
        ).strip()

    def _execute_tool_block(
        self,
        block: Any,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict:
        tool_trace = None
        if self.execution_tracker is not None and run_id and agent_id:
            tool_trace = self.execution_tracker.start_tool_call(
                run_id=run_id,
                agent_id=agent_id,
                tool_name=block.name,
                provider_tool_use_id=getattr(block, "id", None),
            )
        blocked = self.deps.trigger_hooks("PreToolUse", block)
        if blocked:
            output = str(blocked)
            if tool_trace is not None:
                self.execution_tracker.finish_tool_call(
                    tool_trace.id, status=ToolCallStatus.BLOCKED
                )
        else:
            handler = self.handlers.get(block.name)
            output = self.deps.call_tool_handler(
                handler,
                block.input,
                block.name,
            )
            self.deps.trigger_hooks("PostToolUse", block, output)
            if tool_trace is not None:
                self.execution_tracker.finish_tool_call(
                    tool_trace.id,
                    status=ToolCallStatus.SUCCEEDED,
                    metadata={"output_chars": len(str(output))},
                )

        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": str(output),
        }

    def run(self, description: str) -> str:
        tracker = self.execution_tracker
        if tracker is None:
            return self._run_impl(description, None, None)
        run, agent = tracker.start_run(
            agent_kind=AgentKind.SUBAGENT,
            agent_name="subagent",
            role="focused delegated task",
        )
        try:
            result = self._run_impl(description, run.id, agent.id)
        except Exception as exc:
            tracker.emit(
                RuntimeEventType.ERROR,
                run_id=run.id,
                agent_id=agent.id,
                payload={"error_type": type(exc).__name__},
            )
            tracker.finish_run(
                run.id, status=ExecutionStatus.FAILED, error_type=type(exc).__name__
            )
            raise
        tracker.finish_run(run.id, status=ExecutionStatus.SUCCEEDED)
        return result

    def _run_impl(
        self,
        description: str,
        run_id: str | None,
        agent_id: str | None,
    ) -> str:
        messages = [{"role": "user", "content": description}]

        for _ in range(self.config.max_steps):
            model_started = time.monotonic()
            if self.execution_tracker is not None and run_id and agent_id:
                self.execution_tracker.emit(
                    RuntimeEventType.MODEL_CALL_STARTED,
                    run_id=run_id,
                    agent_id=agent_id,
                    payload={"max_tokens": self.config.max_tokens},
                )
            response = self.deps.create_message(
                system=self.config.system_prompt,
                messages=messages,
                tools=SUBAGENT_TOOLS,
                max_tokens=self.config.max_tokens,
            )
            if self.execution_tracker is not None and run_id and agent_id:
                payload = response_usage_payload(response)
                payload.update(
                    {
                        "latency_ms": round((time.monotonic() - model_started) * 1000.0, 3),
                        "retry_count": 0,
                    }
                )
                self.execution_tracker.emit(
                    RuntimeEventType.MODEL_CALL_FINISHED,
                    run_id=run_id,
                    agent_id=agent_id,
                    payload=payload,
                )
            messages.append({"role": "assistant", "content": response.content})

            if not self._has_tool_use(response.content):
                break

            results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                results.append(
                    self._execute_tool_block(
                        block, run_id=run_id, agent_id=agent_id
                    )
                )
            messages.append({"role": "user", "content": results})

        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue
            text = self._extract_text(msg.get("content"))
            if text:
                return text

        return "Subagent finished without a text summary."
