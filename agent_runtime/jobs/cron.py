from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Iterable, Mapping, Any

from .models import CronScheduleRecord, JobRecord, JobStatus
from .store import JobStore


CRON_JOB_SOURCE = "cron"
CRON_JOB_KIND = "scheduled_prompt"


def cron_occurrence_job_id(schedule_id: str, marker: str) -> str:
    """Build a deterministic idempotency key for one cron firing."""
    safe_schedule = re.sub(r"[^A-Za-z0-9_.-]+", "_", schedule_id).strip("_")
    safe_marker = re.sub(r"[^0-9]+", "", marker)
    if not safe_schedule:
        raise ValueError("schedule_id must contain at least one safe character")
    if not safe_marker:
        raise ValueError("marker must contain a timestamp")
    return f"cronfire_{safe_schedule}_{safe_marker}"


def enqueue_scheduled_prompt(
    store: JobStore,
    *,
    schedule_id: str,
    cron: str,
    prompt: str,
    marker: str,
    fired_at: datetime | None = None,
) -> tuple[JobRecord, bool]:
    """Persist one cron occurrence as a durable queued Job.

    The deterministic job id makes dispatch idempotent across scheduler loops
    and process restarts within the same cron minute.
    """
    job_id = cron_occurrence_job_id(schedule_id, marker)
    payload = {
        "schedule_id": schedule_id,
        "cron": cron,
        "prompt": prompt,
        "marker": marker,
    }
    if fired_at is not None:
        payload["fired_at"] = fired_at.isoformat()

    return store.create_job_once(
        CRON_JOB_KIND,
        payload,
        job_id=job_id,
        source=CRON_JOB_SOURCE,
        schedule_id=schedule_id,
        # Scheduled prompts are safe to redeliver after a crash. The runtime
        # therefore uses at-least-once semantics rather than silently dropping
        # an interrupted firing.
        max_attempts=2,
    )




def schedule_has_fired(store: JobStore, schedule_id: str) -> bool:
    """Return whether this schedule already has durable cron occurrence history."""
    return bool(
        store.list_jobs(
            source=CRON_JOB_SOURCE,
            kind=CRON_JOB_KIND,
            schedule_id=schedule_id,
        )
    )

def claim_scheduled_prompts(store: JobStore, runner_id: str) -> list[JobRecord]:
    """Atomically claim all currently queued cron prompt jobs for the main agent."""
    claimed: list[JobRecord] = []
    queued = store.list_jobs(
        status=JobStatus.QUEUED,
        source=CRON_JOB_SOURCE,
        kind=CRON_JOB_KIND,
    )
    for job in queued:
        owned = store.claim_job(job.id, runner_id)
        if owned is not None:
            claimed.append(owned)
    return claimed


def migrate_legacy_schedule_items(
    store: JobStore,
    items: Iterable[Mapping[str, Any]],
    *,
    validate_cron: Callable[[str], str | None],
) -> tuple[int, int]:
    """Import legacy JSON cron definitions into the SQLite schedules table.

    Returns ``(imported, skipped)``. Existing ids are treated idempotently.
    Invalid definitions and one-shot schedules that already have durable firing
    history are skipped rather than resurrected.
    """
    imported = 0
    skipped = 0
    for item in items:
        try:
            schedule_id = str(item["id"])
            cron = str(item["cron"])
            prompt = str(item["prompt"])
            recurring = bool(item.get("recurring", True))
            durable = bool(item.get("durable", True))
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue

        if validate_cron(cron):
            skipped += 1
            continue
        if not recurring and schedule_has_fired(store, schedule_id):
            skipped += 1
            continue

        # Legacy JSON only persisted durable schedules. If an unexpected
        # non-durable record appears, make it durable rather than inventing a
        # stale session identity during migration.
        durable = True if not durable else durable
        try:
            _schedule, created = store.create_schedule_once(
                cron,
                prompt,
                schedule_id=schedule_id,
                recurring=recurring,
                durable=durable,
            )
        except Exception:
            raise
        if created:
            imported += 1
    return imported, skipped


def reconcile_one_shot_schedules(
    store: JobStore,
    schedules: Iterable[CronScheduleRecord],
) -> int:
    """Deactivate active one-shot schedules that already have firing history."""
    reconciled = 0
    for schedule in schedules:
        if schedule.recurring or not schedule.active:
            continue
        if schedule_has_fired(store, schedule.id):
            store.deactivate_schedule(schedule.id)
            reconciled += 1
    return reconciled
