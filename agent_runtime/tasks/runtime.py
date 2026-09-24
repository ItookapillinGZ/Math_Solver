from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .models import TaskRecord
from .store import TaskStore


@dataclass
class Task:
    """Compatibility view used by the application/runtime orchestration layer.

    ``TaskStore`` keeps the durable snake_case ``TaskRecord`` model.  The
    compatibility view preserves the historic ``blockedBy`` attribute expected
    by the teaching/runtime code while also retaining creation timestamps used
    by affinity ordering.
    """

    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    task_type: str = "general"
    required_roles: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    worktree: str | None = None
    lease_until: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class TaskRuntimeConfig:
    legacy_tasks_dir: Path
    default_owner: str = "agent"


@dataclass(frozen=True)
class TaskRuntimeDependencies:
    store: TaskStore
    terminal_print: Callable[[str], None]
    id_factory: Callable[[], str] | None = None
    transition_sink: Callable[[dict], None] | None = None


class TaskRuntime:
    """Application-facing task lifecycle over the durable ``TaskStore``.

    The store owns SQLite transactions and leases.  This runtime owns the
    compatibility view, startup migration/recovery, human-readable tool
    responses, and terminal events used by the agent application.
    """

    def __init__(
        self,
        config: TaskRuntimeConfig,
        dependencies: TaskRuntimeDependencies,
    ) -> None:
        self.config = config
        self.deps = dependencies
        self.store = dependencies.store
        self._id_factory = dependencies.id_factory or self._default_id_factory

    @staticmethod
    def _default_id_factory() -> str:
        return f"task_{int(time.time())}_{random.randint(0, 9999):04d}"

    def _emit_transition(self, transition: str, task: Task, **metadata) -> None:
        sink = self.deps.transition_sink
        if sink is None:
            return
        payload = {
            "task_id": task.id,
            "transition": transition,
            "status": task.status,
            "owner": task.owner,
            "metadata": {
                "task_type": task.task_type,
                "required_roles": list(task.required_roles),
                "tags": list(task.tags),
                **metadata,
            },
        }
        try:
            sink(payload)
        except Exception:
            # Observability must not change task lifecycle behavior.
            pass

    @staticmethod
    def from_record(record: TaskRecord) -> Task:
        return Task(
            id=record.id,
            subject=record.subject,
            description=record.description,
            status=record.status,
            owner=record.owner,
            blockedBy=list(record.blocked_by),
            task_type=record.task_type,
            required_roles=list(record.required_roles),
            tags=list(record.tags),
            worktree=record.worktree,
            lease_until=record.lease_until,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def initialize(self) -> None:
        imported, errors = self.store.import_legacy_json_dir(
            self.config.legacy_tasks_dir
        )
        if imported:
            self.deps.terminal_print(
                f"  \033[34m[tasks] migrated {imported} legacy JSON task(s) "
                "to SQLite\033[0m"
            )
        for error in errors:
            self.deps.terminal_print(
                f"  \033[33m[tasks] migration warning: {error}\033[0m"
            )
        self.recover_expired_task_leases()

    def create_task(
        self,
        subject: str,
        description: str = "",
        blockedBy: list[str] | None = None,
        task_type: str = "general",
        required_roles: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> Task:
        record = self.store.create_task(
            task_id=self._id_factory(),
            subject=subject,
            description=description,
            blocked_by=blockedBy or [],
            task_type=task_type,
            required_roles=required_roles or [],
            tags=tags or [],
        )
        task = self.from_record(record)
        self._emit_transition("created", task)
        return task

    def load_task(self, task_id: str) -> Task:
        record = self.store.get_task(task_id)
        if record is None:
            raise FileNotFoundError(task_id)
        return self.from_record(record)

    def list_tasks(self) -> list[Task]:
        return [self.from_record(record) for record in self.store.list_tasks()]

    def get_task_json(self, task_id: str) -> str:
        return json.dumps(asdict(self.load_task(task_id)), indent=2)

    def can_start(self, task_id: str) -> bool:
        return self.store.can_start(task_id)

    def claim_task(self, task_id: str, owner: str | None = None) -> str:
        claim_owner = owner or self.config.default_owner
        result = self.store.claim_task(task_id, owner=claim_owner)
        if result.claimed and result.task is not None:
            task = self.from_record(result.task)
            self.deps.terminal_print(
                f"  \033[36m[claim] {task.subject} → in_progress\033[0m"
            )
            self._emit_transition("claimed", task)
            return f"Claimed {task.id} ({task.subject})"

        if result.task is None:
            raise FileNotFoundError(task_id)

        if result.blocked_by or result.missing_dependencies:
            parts: list[str] = []
            if result.blocked_by:
                parts.append(f"blocked by: {list(result.blocked_by)}")
            if result.missing_dependencies:
                parts.append(
                    f"missing deps: {list(result.missing_dependencies)}"
                )
            return "Cannot start — " + ", ".join(parts)

        return result.reason

    def heartbeat_task(self, task_id: str, owner: str | None = None) -> str:
        heartbeat_owner = owner or self.config.default_owner
        record = self.store.heartbeat_task(task_id, owner=heartbeat_owner)
        task = self.from_record(record)
        self._emit_transition("heartbeat", task, lease_until=task.lease_until)
        return f"Heartbeat renewed for {task.id} until {task.lease_until}"

    def recover_expired_task_leases(self, *, log: bool = True) -> list[Task]:
        recovered = [
            self.from_record(record)
            for record in self.store.recover_expired_tasks()
        ]
        for task in recovered:
            self._emit_transition("lease_recovered", task)
        if recovered and log:
            names = ", ".join(task.subject for task in recovered)
            self.deps.terminal_print(
                f"  \033[33m[lease] recovered {len(recovered)} expired "
                f"task(s): {names}\033[0m"
            )
        return recovered

    def recover_expired_tasks_text(self) -> str:
        recovered = self.recover_expired_task_leases(log=False)
        if not recovered:
            return "No expired task leases."
        return "Recovered expired tasks: " + ", ".join(
            f"{task.id} ({task.subject})" for task in recovered
        )

    def complete_task(self, task_id: str, owner: str | None = None) -> str:
        task = self.load_task(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"

        record = self.store.complete_task(
            task_id, owner=owner or self.config.default_owner
        )
        completed = self.from_record(record)
        unblocked = [
            candidate.subject
            for candidate in self.list_tasks()
            if candidate.status == "pending"
            and candidate.blockedBy
            and self.can_start(candidate.id)
        ]
        self.deps.terminal_print(
            f"  \033[32m[complete] {completed.subject} ✓\033[0m"
        )
        self._emit_transition("completed", completed)
        message = f"Completed {completed.id} ({completed.subject})"
        if unblocked:
            message += f"\nUnblocked: {', '.join(unblocked)}"
        return message

    # Tool-facing text helpers -------------------------------------------------
    def create_task_text(
        self,
        subject: str,
        description: str = "",
        blockedBy: list[str] | None = None,
        task_type: str = "general",
        required_roles: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> str:
        task = self.create_task(
            subject,
            description,
            blockedBy,
            task_type=task_type,
            required_roles=required_roles,
            tags=tags,
        )
        deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
        affinity = f" [type={task.task_type}"
        if task.required_roles:
            affinity += f", roles={task.required_roles}"
        affinity += "]"
        self.deps.terminal_print(
            f"  \033[34m[create] {task.subject}{deps}{affinity}\033[0m"
        )
        return f"Created {task.id}: {task.subject}{deps}{affinity}"

    def list_tasks_text(self) -> str:
        tasks = self.list_tasks()
        if not tasks:
            return "No tasks."
        return "\n".join(
            f"  {task.id}: {task.subject} [{task.status}] "
            f"type={task.task_type}"
            + (f" roles={task.required_roles}" if task.required_roles else "")
            + (f" tags={task.tags}" if task.tags else "")
            + (f" (wt:{task.worktree})" if task.worktree else "")
            for task in tasks
        )

    def get_task_text(self, task_id: str) -> str:
        try:
            return self.get_task_json(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"

    def claim_task_text(self, task_id: str) -> str:
        try:
            task = self.load_task(task_id)
            if "research_workflow" in task.tags:
                return (
                    f"Cannot claim task {task_id}: research proof tasks "
                    "belong to the assigned teammate"
                )
            return self.claim_task(task_id, owner=self.config.default_owner)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"

    def complete_task_text(self, task_id: str) -> str:
        try:
            task = self.load_task(task_id)
            if "research_workflow" in task.tags:
                return (
                    f"Cannot complete task {task_id}: research proof tasks "
                    "must be completed by the assigned teammate"
                )
            return self.complete_task(task_id, owner=self.config.default_owner)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
        except (PermissionError, RuntimeError, ValueError) as exc:
            return f"Error: {exc}"

    def heartbeat_task_text(self, task_id: str) -> str:
        try:
            return self.heartbeat_task(task_id, owner=self.config.default_owner)
        except (FileNotFoundError, KeyError):
            return f"Error: task {task_id} not found"
        except (PermissionError, RuntimeError, ValueError) as exc:
            return f"Error: {exc}"
