from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


TaskStatus = Literal["pending", "in_progress", "completed", "failed", "cancelled"]
TaskType = Literal["general", "math", "engineering", "literature"]


@dataclass(frozen=True)
class TaskRecord:
    id: str
    subject: str
    description: str
    status: TaskStatus
    owner: str | None
    blocked_by: tuple[str, ...]
    task_type: str = "general"
    required_roles: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    worktree: str | None = None
    lease_until: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    task: TaskRecord | None
    reason: str
    blocked_by: tuple[str, ...] = ()
    missing_dependencies: tuple[str, ...] = ()
