from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import threading
import uuid
from typing import Any, Callable


def response_usage_payload(response: Any) -> dict[str, Any]:
    """Extract safe provider usage metadata without depending on SDK types."""
    usage = getattr(response, "usage", None)

    def get(name: str, default: int = 0):
        if isinstance(usage, dict):
            return usage.get(name, default)
        return getattr(usage, name, default) if usage is not None else default

    return {
        "model": str(getattr(response, "model", "") or ""),
        "stop_reason": str(getattr(response, "stop_reason", "") or ""),
        "input_tokens": int(get("input_tokens") or 0),
        "output_tokens": int(get("output_tokens") or 0),
        "cache_creation_input_tokens": int(get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": int(get("cache_read_input_tokens") or 0),
    }


class AgentKind(str, Enum):
    LEAD = "lead"
    SUBAGENT = "subagent"
    TEAMMATE = "teammate"


class ExecutionStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolCallStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    BACKGROUND = "background"


class RuntimeEventType(str, Enum):
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    AGENT_STARTED = "agent_started"
    AGENT_FINISHED = "agent_finished"
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_CALL_FINISHED = "model_call_finished"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    ERROR = "error"


@dataclass(frozen=True)
class AgentRecord:
    id: str
    kind: AgentKind
    name: str
    role: str = ""
    parent_agent_id: str | None = None
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "role": self.role,
            "parent_agent_id": self.parent_agent_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class RunRecord:
    id: str
    agent_id: str
    status: ExecutionStatus
    started_at: str
    ended_at: str | None = None
    parent_run_id: str | None = None
    error_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "parent_run_id": self.parent_run_id,
            "error_type": self.error_type,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ToolCallRecord:
    id: str
    run_id: str
    agent_id: str
    tool_name: str
    status: ToolCallStatus
    started_at: str
    ended_at: str | None = None
    provider_tool_use_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "provider_tool_use_id": self.provider_tool_use_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class RuntimeEvent:
    id: str
    type: RuntimeEventType
    timestamp: str
    run_id: str
    agent_id: str | None = None
    tool_call_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "tool_call_id": self.tool_call_id,
            "task_id": self.task_id,
            "payload": dict(self.payload),
        }


class ExecutionTracker:
    """Thread-safe, in-memory execution model for runtime instrumentation.

    This is deliberately *not* the final observability backend. It establishes
    one shared vocabulary for lead, subagent, and teammate execution while
    keeping persistence optional. Integrations should record only safe metadata;
    raw prompts, raw tool inputs, and full tool outputs are not captured by the
    tracker itself.
    """

    def __init__(
        self,
        *,
        event_sink: Callable[[RuntimeEvent], None] | None = None,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[str], str] | None = None,
        max_events: int = 2000,
    ):
        self._event_sink = event_sink
        self._clock = clock or self._utc_now
        self._id_factory = id_factory or self._new_id
        self._max_events = max(1, int(max_events))
        self._lock = threading.RLock()
        self._thread_context = threading.local()
        self._runs: dict[str, RunRecord] = {}
        self._agents: dict[str, AgentRecord] = {}
        self._tool_calls: dict[str, ToolCallRecord] = {}
        self._events: deque[RuntimeEvent] = deque(maxlen=self._max_events)

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    def _context_stack(self) -> list[tuple[str, str]]:
        stack = getattr(self._thread_context, "stack", None)
        if stack is None:
            stack = []
            self._thread_context.stack = stack
        return stack

    def current_run_id(self) -> str | None:
        stack = self._context_stack()
        return stack[-1][0] if stack else None

    def current_agent_id(self) -> str | None:
        stack = self._context_stack()
        return stack[-1][1] if stack else None

    def capture_parent(self) -> tuple[str | None, str | None]:
        return self.current_run_id(), self.current_agent_id()

    def start_run(
        self,
        *,
        agent_kind: AgentKind | str,
        agent_name: str,
        role: str = "",
        parent_run_id: str | None = None,
        parent_agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[RunRecord, AgentRecord]:
        if parent_run_id is None:
            parent_run_id = self.current_run_id()
        if parent_agent_id is None:
            parent_agent_id = self.current_agent_id()
        now = self._clock()
        agent = AgentRecord(
            id=self._id_factory("agent"),
            kind=AgentKind(agent_kind),
            name=str(agent_name),
            role=str(role),
            parent_agent_id=parent_agent_id,
            created_at=now,
            metadata=dict(metadata or {}),
        )
        run = RunRecord(
            id=self._id_factory("run"),
            agent_id=agent.id,
            status=ExecutionStatus.RUNNING,
            started_at=now,
            parent_run_id=parent_run_id,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._agents[agent.id] = agent
            self._runs[run.id] = run
        self._context_stack().append((run.id, agent.id))
        self.emit(
            RuntimeEventType.AGENT_STARTED,
            run_id=run.id,
            agent_id=agent.id,
            payload={"kind": agent.kind.value, "name": agent.name, "role": agent.role},
        )
        self.emit(
            RuntimeEventType.RUN_STARTED,
            run_id=run.id,
            agent_id=agent.id,
            payload={"parent_run_id": parent_run_id},
        )
        return run, agent

    def finish_run(
        self,
        run_id: str,
        *,
        status: ExecutionStatus | str = ExecutionStatus.SUCCEEDED,
        error_type: str | None = None,
    ) -> RunRecord:
        final_status = ExecutionStatus(status)
        now = self._clock()
        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise KeyError(run_id)
            if current.status is not ExecutionStatus.RUNNING:
                return current
            updated = replace(
                current,
                status=final_status,
                ended_at=now,
                error_type=error_type,
            )
            self._runs[run_id] = updated
            agent_id = updated.agent_id
        stack = self._context_stack()
        if stack and stack[-1][0] == run_id:
            stack.pop()
        self.emit(
            RuntimeEventType.RUN_FINISHED,
            run_id=run_id,
            agent_id=agent_id,
            payload={"status": final_status.value, "error_type": error_type},
        )
        self.emit(
            RuntimeEventType.AGENT_FINISHED,
            run_id=run_id,
            agent_id=agent_id,
            payload={"status": final_status.value},
        )
        return updated

    def start_tool_call(
        self,
        *,
        run_id: str,
        agent_id: str,
        tool_name: str,
        provider_tool_use_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ToolCallRecord:
        record = ToolCallRecord(
            id=self._id_factory("tool"),
            run_id=run_id,
            agent_id=agent_id,
            tool_name=str(tool_name),
            status=ToolCallStatus.RUNNING,
            started_at=self._clock(),
            provider_tool_use_id=provider_tool_use_id,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(run_id)
            if agent_id not in self._agents:
                raise KeyError(agent_id)
            self._tool_calls[record.id] = record
        self.emit(
            RuntimeEventType.TOOL_CALL_STARTED,
            run_id=run_id,
            agent_id=agent_id,
            tool_call_id=record.id,
            payload={"tool_name": record.tool_name},
        )
        return record

    def finish_tool_call(
        self,
        tool_call_id: str,
        *,
        status: ToolCallStatus | str = ToolCallStatus.SUCCEEDED,
        metadata: dict[str, Any] | None = None,
    ) -> ToolCallRecord:
        final_status = ToolCallStatus(status)
        now = self._clock()
        with self._lock:
            current = self._tool_calls.get(tool_call_id)
            if current is None:
                raise KeyError(tool_call_id)
            if current.status is not ToolCallStatus.RUNNING:
                return current
            merged_metadata = dict(current.metadata)
            merged_metadata.update(metadata or {})
            updated = replace(
                current,
                status=final_status,
                ended_at=now,
                metadata=merged_metadata,
            )
            self._tool_calls[tool_call_id] = updated
        self.emit(
            RuntimeEventType.TOOL_CALL_FINISHED,
            run_id=updated.run_id,
            agent_id=updated.agent_id,
            tool_call_id=updated.id,
            payload={"tool_name": updated.tool_name, "status": final_status.value},
        )
        return updated

    def emit(
        self,
        event_type: RuntimeEventType | str,
        *,
        run_id: str,
        agent_id: str | None = None,
        tool_call_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        event = RuntimeEvent(
            id=self._id_factory("evt"),
            type=RuntimeEventType(event_type),
            timestamp=self._clock(),
            run_id=run_id,
            agent_id=agent_id,
            tool_call_id=tool_call_id,
            task_id=task_id,
            payload=dict(payload or {}),
        )
        with self._lock:
            self._events.append(event)
        if self._event_sink is not None:
            try:
                self._event_sink(event)
            except Exception:
                # Observability must never change runtime behavior.
                pass
        return event

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        with self._lock:
            return self._agents.get(agent_id)

    def get_tool_call(self, tool_call_id: str) -> ToolCallRecord | None:
        with self._lock:
            return self._tool_calls.get(tool_call_id)

    def list_runs(self) -> list[RunRecord]:
        with self._lock:
            return list(self._runs.values())

    def list_agents(self) -> list[AgentRecord]:
        with self._lock:
            return list(self._agents.values())

    def list_tool_calls(self) -> list[ToolCallRecord]:
        with self._lock:
            return list(self._tool_calls.values())

    def list_events(self) -> list[RuntimeEvent]:
        with self._lock:
            return list(self._events)
