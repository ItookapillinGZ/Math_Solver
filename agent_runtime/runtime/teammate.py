from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .execution import (
    AgentKind,
    ExecutionStatus,
    ExecutionTracker,
    RuntimeEventType,
    ToolCallStatus,
    response_usage_payload,
)


TEAMMATE_TOOLS = [
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
        "name": "verify_math_symbolic",
        "description": (
            "Run restricted SymPy verification in an isolated child process. "
            "Use it for algebraic identities, derivatives, integrals, limits, "
            "and simplification. Imports, file/network/process access, and "
            "Python introspection are unavailable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sympy_code": {
                    "type": "string",
                    "description": (
                        "The pure Python code using SymPy. Always assign the final "
                        "mathematical result you want to see to a variable named "
                        "'result'."
                    ),
                }
            },
            "required": ["sympy_code"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file.",
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
        "description": "Write file.",
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
        "name": "send_message",
        "description": "Send message to another agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["to", "content"],
        },
    },
    {
        "name": "submit_plan",
        "description": "Submit a plan for Lead approval.",
        "input_schema": {
            "type": "object",
            "properties": {"plan": {"type": "string"}},
            "required": ["plan"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List all tasks.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "claim_task",
        "description": "Claim a pending task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "complete_task",
        "description": "Mark an in-progress task as completed.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]


def _start_daemon_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True).start()


@dataclass(frozen=True)
class TeammateRuntimeDependencies:
    """Concrete dependencies required by the autonomous teammate runtime.

    The runtime owns orchestration only. Domain prompts, task persistence,
    communication, filesystem tools, and model invocation stay injected so this
    module does not import the application entrypoint or Anthropic directly.
    """

    application: Any
    coordinator: Any
    list_tasks: Callable[[], list[Any]]
    load_task: Callable[[str], Any]
    can_start: Callable[[str], bool]
    claim_task: Callable[..., str]
    complete_task: Callable[..., str]
    recover_expired_task_leases: Callable[..., Any]
    task_matches_worker: Callable[[Any, str], bool]
    select_tasks_for_worker: Callable[[list[Any], str], Any]
    heartbeat_task: Callable[..., Any]
    worktrees_dir: Path
    run_bash: Callable[..., str]
    run_read: Callable[..., str]
    run_write: Callable[..., str]
    call_tool_handler: Callable[[Any, dict, str], str]
    has_tool_use: Callable[[Any], bool]
    create_message: Callable[..., Any]
    terminal_print: Callable[[str], None]
    verify_math_symbolic: Callable[[str], str] | None = None
    heartbeat_interval: float = 60.0
    idle_poll_interval: float = 5.0
    idle_timeout: float = 60.0
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    start_thread: Callable[[Callable[[], None]], None] = _start_daemon_thread


@dataclass
class _TaskContext:
    task_id: str | None = None
    last_heartbeat: float = 0.0
    proof_path: Path | None = None
    proof_digest: bytes | None = None


@dataclass
class _WorktreeContext:
    path: str | None = None


class TeammateRuntime:
    """Runs autonomous worker teammates behind the Lead agent.

    Responsibilities:
    - maintain active teammate lifecycle state;
    - poll inbox before scanning eligible tasks;
    - enforce role affinity for autonomous claims;
    - maintain task lease heartbeats and worktree context;
    - gate execution after a submitted plan until Lead approval arrives;
    - execute the teammate's local tool loop and report a final result.

    The class deliberately does not own prompts, TeamCoordinator persistence,
    TaskStore persistence, or Anthropic configuration.
    """

    def __init__(
        self,
        dependencies: TeammateRuntimeDependencies,
        *,
        execution_tracker: ExecutionTracker | None = None,
    ):
        self.deps = dependencies
        self.execution_tracker = execution_tracker
        self.active_teammates: dict[str, bool] = {}
        self._active_lock = threading.RLock()

    def scan_unclaimed_tasks(self, role: str | None = None) -> list[Any]:
        self.deps.recover_expired_task_leases(log=False)
        candidates = [
            task
            for task in self.deps.list_tasks()
            if task.status == "pending"
            and not task.owner
            and self.deps.can_start(task.id)
        ]
        if role is not None:
            candidates = list(self.deps.select_tasks_for_worker(candidates, role))
        return candidates

    def idle_poll(
        self,
        agent_name: str,
        messages: list,
        role: str,
        worktree_context: _WorktreeContext | None = None,
        on_claim: Callable[[str], None] | None = None,
    ) -> str:
        polls = max(1, int(self.deps.idle_timeout // self.deps.idle_poll_interval))
        for _ in range(polls):
            self.deps.sleep(self.deps.idle_poll_interval)
            inbox = self.deps.coordinator.bus.read_inbox(agent_name)
            if inbox:
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        req_id = msg.get("metadata", {}).get("request_id", "")
                        self.deps.coordinator.bus.send(
                            agent_name,
                            "lead",
                            "Shutting down.",
                            "shutdown_response",
                            {"request_id": req_id, "approve": True},
                        )
                        return "shutdown"
                messages.append(
                    {
                        "role": "user",
                        "content": "<inbox>" + json.dumps(inbox) + "</inbox>",
                    }
                )
                return "work"

            unclaimed = self.scan_unclaimed_tasks(role)
            if not unclaimed:
                continue

            task = unclaimed[0]
            result = self.deps.claim_task(task.id, owner=agent_name)
            if "Claimed" not in result:
                continue

            if on_claim is not None:
                on_claim(task.id)

            wt_info = ""
            if task.worktree:
                wt_path = self.deps.worktrees_dir / task.worktree
                wt_info = f"\nWork directory: {wt_path}"
                if worktree_context is not None:
                    worktree_context.path = str(wt_path)

            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"<auto-claimed>Task {task.id}: {task.subject}"
                        f"{wt_info}</auto-claimed>"
                    ),
                }
            )
            return "work"
        return "timeout"

    def spawn(self, name: str, role: str, prompt: str) -> str:
        with self._active_lock:
            if name in self.active_teammates:
                return f"Teammate '{name}' already exists"
            self.active_teammates[name] = True

        parent_run_id = None
        parent_agent_id = None
        if self.execution_tracker is not None:
            parent_run_id, parent_agent_id = self.execution_tracker.capture_parent()

        try:
            self.deps.start_thread(
                lambda: self._run_teammate(
                    name=name,
                    role=role,
                    prompt=prompt,
                    parent_run_id=parent_run_id,
                    parent_agent_id=parent_agent_id,
                )
            )
        except Exception:
            with self._active_lock:
                self.active_teammates.pop(name, None)
            raise
        return f"Teammate '{name}' spawned as {role}"

    def _handle_inbox_message(
        self,
        *,
        name: str,
        msg: dict,
        messages: list,
        protocol_ctx: dict,
    ) -> bool:
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")

        if msg_type == "shutdown_request":
            self.deps.coordinator.bus.send(
                name,
                "lead",
                "Shutting down.",
                "shutdown_response",
                {"request_id": req_id, "approve": True},
            )
            return True

        if msg_type == "plan_approval_response":
            approve = meta.get("approve", False)
            if req_id == protocol_ctx.get("waiting_plan"):
                protocol_ctx["waiting_plan"] = None
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "[Plan approved]"
                        if approve
                        else f"[Plan rejected] {msg.get('content', '')}"
                    ),
                }
            )
        return False

    def _run_teammate(
        self,
        *,
        name: str,
        role: str,
        prompt: str,
        parent_run_id: str | None = None,
        parent_agent_id: str | None = None,
    ) -> None:
        trace_run = None
        trace_agent = None
        if self.execution_tracker is not None:
            trace_run, trace_agent = self.execution_tracker.start_run(
                agent_kind=AgentKind.TEAMMATE,
                agent_name=name,
                role=role,
                parent_run_id=parent_run_id,
                parent_agent_id=parent_agent_id,
            )
        protocol_ctx = {"waiting_plan": None}
        system = self.deps.application.build_teammate_system_prompt(name, role)
        worktree_ctx = _WorktreeContext()
        task_ctx = _TaskContext()
        messages = [{"role": "user", "content": prompt}]

        def proof_digest(path: Path) -> bytes | None:
            try:
                return hashlib.sha256(path.read_bytes()).digest()
            except FileNotFoundError:
                return None

        def mark_claimed(task_id: str) -> None:
            task = self.deps.load_task(task_id)
            task_ctx.task_id = task_id
            task_ctx.last_heartbeat = self.deps.monotonic()
            task_ctx.proof_path = None
            task_ctx.proof_digest = None
            if "research_workflow" not in getattr(task, "tags", []):
                return
            if not task.worktree:
                return
            description = getattr(task, "description", "")
            relative_path = None
            for line in description.splitlines():
                if line.startswith("Cumulative proof file: "):
                    relative_path = line.removeprefix("Cumulative proof file: ").strip()
                    break
                if line.startswith("Revise the cumulative proof at "):
                    relative_path = line.removeprefix(
                        "Revise the cumulative proof at "
                    ).rstrip(".").strip()
                    break
            if not relative_path:
                return
            worktree = (self.deps.worktrees_dir / task.worktree).resolve()
            candidate = (worktree / relative_path).resolve()
            if not candidate.is_relative_to(worktree):
                return
            task_ctx.proof_path = candidate
            task_ctx.proof_digest = proof_digest(candidate)

        def clear_claimed(task_id: str | None = None) -> None:
            if task_id is None or task_ctx.task_id == task_id:
                task_ctx.task_id = None
                task_ctx.last_heartbeat = 0.0
                task_ctx.proof_path = None
                task_ctx.proof_digest = None
                worktree_ctx.path = None

        def auto_heartbeat() -> None:
            if not task_ctx.task_id:
                return
            elapsed = self.deps.monotonic() - task_ctx.last_heartbeat
            if elapsed < self.deps.heartbeat_interval:
                return
            task_id = task_ctx.task_id
            try:
                self.deps.heartbeat_task(task_id, owner=name)
                task_ctx.last_heartbeat = self.deps.monotonic()
            except Exception as exc:
                self.deps.terminal_print(
                    f"  \033[33m[{name} lease] lost {task_id}: {exc}\033[0m"
                )
                clear_claimed(task_id)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"<task-lease-lost>{task_id}: {exc}. "
                            "Stop modifying its worktree and claim an available "
                            "task before continuing.</task-lease-lost>"
                        ),
                    }
                )

        def worktree_cwd() -> Path | None:
            return Path(worktree_ctx.path) if worktree_ctx.path else None

        def run_bash(command: str) -> str:
            return self.deps.run_bash(command, cwd=worktree_cwd())

        def run_read(
            path: str,
            limit: int | None = None,
            offset: int = 0,
        ) -> str:
            return self.deps.run_read(
                path,
                limit=limit,
                offset=offset,
                cwd=worktree_cwd(),
            )

        def run_write(path: str, content: str) -> str:
            return self.deps.run_write(path, content, cwd=worktree_cwd())

        def run_list_tasks() -> str:
            tasks = [
                task
                for task in self.deps.list_tasks()
                if task.owner == name
                or (
                    task.status == "pending"
                    and task.owner is None
                    and self.deps.can_start(task.id)
                    and self.deps.task_matches_worker(task, role)
                )
            ]
            if not tasks:
                return f"No tasks eligible for role: {role}."
            return "\n".join(
                f"  {task.id}: {task.subject} [{task.status}] "
                f"type={task.task_type}"
                + (
                    f" roles={task.required_roles}"
                    if task.required_roles
                    else ""
                )
                + (f" (wt:{task.worktree})" if task.worktree else "")
                for task in tasks
            )

        def run_claim_task(task_id: str) -> str:
            try:
                candidate = self.deps.load_task(task_id)
            except FileNotFoundError:
                return f"Error: task {task_id} not found"

            if (
                candidate.status == "pending"
                and candidate.owner is None
                and not self.deps.task_matches_worker(candidate, role)
            ):
                return (
                    f"Cannot claim — task affinity does not match role '{role}'. "
                    f"task_type={candidate.task_type}, "
                    f"required_roles={candidate.required_roles or []}"
                )

            result = self.deps.claim_task(task_id, owner=name)
            if "Claimed" in result:
                task = self.deps.load_task(task_id)
                mark_claimed(task_id)
                worktree_ctx.path = (
                    str(self.deps.worktrees_dir / task.worktree)
                    if task.worktree
                    else None
                )
            return result

        def run_complete_task(task_id: str) -> str:
            try:
                task = self.deps.load_task(task_id)
            except FileNotFoundError:
                return f"Error: task {task_id} not found"
            if "research_workflow" in getattr(task, "tags", []):
                if task_ctx.task_id != task_id or task.owner != name:
                    return (
                        f"Cannot complete task {task_id}: this teammate must "
                        "claim the proof task first"
                    )
                if task_ctx.proof_path is None:
                    return (
                        f"Cannot complete task {task_id}: no proof file is "
                        "bound inside the assigned worktree"
                    )
                try:
                    content = task_ctx.proof_path.read_bytes()
                except FileNotFoundError:
                    return (
                        f"Cannot complete task {task_id}: cumulative proof "
                        "file does not exist"
                    )
                if not content.strip() or (
                    hashlib.sha256(content).digest() == task_ctx.proof_digest
                ):
                    return (
                        f"Cannot complete task {task_id}: update the cumulative "
                        "proof file after claiming this step"
                    )
            try:
                result = self.deps.complete_task(task_id, owner=name)
            except (PermissionError, RuntimeError, ValueError) as exc:
                return f"Error: {exc}"
            if result.startswith("Completed"):
                clear_claimed(task_id)
            return result

        def run_verify_math_symbolic(sympy_code: str) -> str:
            verifier = self.deps.verify_math_symbolic
            if verifier is None:
                return (
                    "Symbolic Execution Failed.\n"
                    "[Error Message]: Safe SymPy executor is not configured."
                )
            return verifier(sympy_code)

        sub_handlers = {
            "bash": run_bash,
            "read_file": run_read,
            "write_file": run_write,
            "send_message": lambda to, content: (
                self.deps.coordinator.bus.send(name, to, content),
                "Sent",
            )[1],
            "verify_math_symbolic": run_verify_math_symbolic,
            "list_tasks": run_list_tasks,
            "claim_task": run_claim_task,
            "complete_task": run_complete_task,
        }

        try:
            while True:
                if len(messages) <= 3:
                    messages.insert(
                        0,
                        {
                            "role": "user",
                            "content": self.deps.application.build_identity_prompt(
                                name, role
                            ),
                        },
                    )

                should_shutdown = False
                for _ in range(10):
                    auto_heartbeat()
                    inbox = self.deps.coordinator.bus.read_inbox(name)
                    for msg in inbox:
                        if self._handle_inbox_message(
                            name=name,
                            msg=msg,
                            messages=messages,
                            protocol_ctx=protocol_ctx,
                        ):
                            should_shutdown = True
                            break
                    if should_shutdown:
                        break

                    if protocol_ctx["waiting_plan"]:
                        self.deps.sleep(self.deps.idle_poll_interval)
                        continue

                    if inbox:
                        ordinary = [
                            msg for msg in inbox if msg.get("type") == "message"
                        ]
                        if ordinary:
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "<inbox>"
                                        + json.dumps(ordinary)
                                        + "</inbox>"
                                    ),
                                }
                            )

                    try:
                        self._sanitize_tool_inputs(messages)
                        model_started = time.monotonic()
                        if (
                            self.execution_tracker is not None
                            and trace_run is not None
                            and trace_agent is not None
                        ):
                            self.execution_tracker.emit(
                                RuntimeEventType.MODEL_CALL_STARTED,
                                run_id=trace_run.id,
                                agent_id=trace_agent.id,
                                payload={"max_tokens": 8000},
                            )
                        response = self.deps.create_message(
                            system=system,
                            messages=messages[-20:],
                            tools=TEAMMATE_TOOLS,
                            max_tokens=8000,
                        )
                        if (
                            self.execution_tracker is not None
                            and trace_run is not None
                            and trace_agent is not None
                        ):
                            payload = response_usage_payload(response)
                            payload.update(
                                {
                                    "latency_ms": round((time.monotonic() - model_started) * 1000.0, 3),
                                    "retry_count": 0,
                                }
                            )
                            self.execution_tracker.emit(
                                RuntimeEventType.MODEL_CALL_FINISHED,
                                run_id=trace_run.id,
                                agent_id=trace_agent.id,
                                payload=payload,
                            )
                    except Exception as exc:
                        if (
                            self.execution_tracker is not None
                            and trace_run is not None
                            and trace_agent is not None
                        ):
                            self.execution_tracker.emit(
                                RuntimeEventType.ERROR,
                                run_id=trace_run.id,
                                agent_id=trace_agent.id,
                                payload={
                                    "stage": "model_call",
                                    "error_type": type(exc).__name__,
                                },
                            )
                        # Account suspension and other permanent client errors
                        # cannot succeed after a delay. Report them once.
                        if self._is_permanent_model_error(exc):
                            self.deps.terminal_print(
                                f"  \033[31m[{name} API Error] {exc}. Stopping teammate.\033[0m"
                            )
                            self.deps.coordinator.bus.send(
                                name, "lead", f"{name} stopped after a permanent model error: {exc}", "result"
                            )
                            raise
                        self.deps.terminal_print(
                            f"  \033[31m[{name} API Error] {exc}. "
                            "Retrying after 3s...\033[0m"
                        )
                        self.deps.sleep(3.0)
                        continue

                    corrected_content = []
                    for block in response.content:
                        if (
                            getattr(block, "type", None) == "tool_use"
                            and not isinstance(getattr(block, "input", None), dict)
                        ):
                            block.input = {}
                        corrected_content.append(block)
                    messages.append(
                        {"role": "assistant", "content": corrected_content}
                    )

                    if not self.deps.has_tool_use(response.content):
                        if isinstance(response.content, list):
                            assistant_text = "".join(
                                block.text
                                for block in response.content
                                if hasattr(block, "text")
                            )
                        else:
                            assistant_text = str(response.content)

                        upper_text = assistant_text.upper()
                        if (
                            "PROOF COMPLETE" in upper_text
                            or "TASK FINISHED" in upper_text
                        ):
                            break
                        messages.append(
                            {
                                "role": "user",
                                "content": self.deps.application.progress_nudge,
                            }
                        )
                        continue

                    results = []
                    for block in response.content:
                        if getattr(block, "type", None) != "tool_use":
                            continue
                        tool_trace = None
                        if (
                            self.execution_tracker is not None
                            and trace_run is not None
                            and trace_agent is not None
                        ):
                            tool_trace = self.execution_tracker.start_tool_call(
                                run_id=trace_run.id,
                                agent_id=trace_agent.id,
                                tool_name=block.name,
                                provider_tool_use_id=getattr(block, "id", None),
                            )
                        if block.name == "submit_plan":
                            output = self.deps.coordinator.submit_plan(
                                name, block.input.get("plan", "")
                            )
                            match = re.search(r"\((req_\d+)\)", output)
                            protocol_ctx["waiting_plan"] = (
                                match.group(1) if match else output
                            )
                        else:
                            handler = sub_handlers.get(block.name)
                            output = self.deps.call_tool_handler(
                                handler, block.input, block.name
                            )
                        if tool_trace is not None:
                            self.execution_tracker.finish_tool_call(
                                tool_trace.id,
                                status=ToolCallStatus.SUCCEEDED,
                                metadata={"output_chars": len(str(output))},
                            )
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(output),
                            }
                        )
                        if protocol_ctx["waiting_plan"]:
                            break

                    messages.append({"role": "user", "content": results})
                    if protocol_ctx["waiting_plan"]:
                        break

                if should_shutdown:
                    break
                if protocol_ctx["waiting_plan"]:
                    continue

                idle_result = self.idle_poll(
                    name,
                    messages,
                    role,
                    worktree_context=worktree_ctx,
                    on_claim=mark_claimed,
                )
                if idle_result in ("shutdown", "timeout"):
                    break

            self.deps.coordinator.bus.send(
                name,
                "lead",
                self._latest_assistant_text(messages),
                "result",
            )
        except Exception as exc:
            if (
                self.execution_tracker is not None
                and trace_run is not None
                and trace_agent is not None
            ):
                self.execution_tracker.emit(
                    RuntimeEventType.ERROR,
                    run_id=trace_run.id,
                    agent_id=trace_agent.id,
                    payload={"error_type": type(exc).__name__},
                )
                self.execution_tracker.finish_run(
                    trace_run.id,
                    status=ExecutionStatus.FAILED,
                    error_type=type(exc).__name__,
                )
            raise
        else:
            if self.execution_tracker is not None and trace_run is not None:
                self.execution_tracker.finish_run(
                    trace_run.id, status=ExecutionStatus.SUCCEEDED
                )
        finally:
            with self._active_lock:
                self.active_teammates.pop(name, None)

    @staticmethod
    def _is_permanent_model_error(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is None:
            match = re.search(r"error code:\s*(4\d\d)", str(exc), re.IGNORECASE)
            status = int(match.group(1)) if match else None
        return isinstance(status, int) and 400 <= status < 500 and status not in {408, 409, 425, 429}

    @staticmethod
    def _sanitize_tool_inputs(messages: list) -> None:
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                value = block.get("input")
                if isinstance(value, str):
                    try:
                        block["input"] = json.loads(value)
                    except Exception:
                        pass
                elif value == []:
                    block["input"] = {}

    @staticmethod
    def _latest_assistant_text(messages: list) -> str:
        for msg in reversed(messages):
            if msg.get("role") != "assistant" or not isinstance(
                msg.get("content"), list
            ):
                continue
            for block in msg["content"]:
                if getattr(block, "type", None) == "text":
                    return block.text
        return "Done."
