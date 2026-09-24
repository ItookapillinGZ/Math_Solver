from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }


@dataclass(frozen=True)
class JobRecord:
    id: str
    kind: str
    status: JobStatus
    payload: dict[str, Any]
    result: Any | None
    error: str | None
    source: str | None
    schedule_id: str | None
    runner_id: str | None
    timeout_seconds: float | None
    max_attempts: int
    attempt_count: int
    available_at: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    notified_at: str | None


@dataclass(frozen=True)
class CronScheduleRecord:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    active: bool
    session_id: str | None
    last_fired_marker: str | None
    created_at: str
    updated_at: str
    cancelled_at: str | None
