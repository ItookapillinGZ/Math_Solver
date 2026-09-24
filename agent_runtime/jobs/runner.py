from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from typing import Any

from .models import JobRecord, JobStatus
from .store import JobStateError, JobStore


JobExecutor = Callable[[JobRecord], Any]


class ThreadJobRunner:
    """Small in-process runner backed by a durable JobStore.

    Callers submit jobs; runner ids are generated internally and are never part
    of the user-facing API. SQLite ownership prevents duplicate execution when
    multiple threads/processes race for the same queued job.
    """

    def __init__(self, store: JobStore, *, runner_prefix: str = "runner"):
        self.store = store
        self.runner_prefix = runner_prefix
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def _new_runner_id(self) -> str:
        return f"{self.runner_prefix}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    def start(self, job_id: str, execute: JobExecutor) -> bool:
        """Start a daemon thread for one queued job.

        ``False`` means this process already has a live thread assigned to the
        same job. A second process may still race; JobStore.claim_job handles it.
        """
        with self._lock:
            existing = self._threads.get(job_id)
            if existing is not None and existing.is_alive():
                return False
            thread = threading.Thread(
                target=self._run,
                args=(job_id, execute),
                daemon=True,
                name=f"job-{job_id}",
            )
            self._threads[job_id] = thread
            thread.start()
            return True

    def _run(self, job_id: str, execute: JobExecutor) -> None:
        runner_id = self._new_runner_id()
        try:
            claimed = self.store.claim_job(job_id, runner_id)
            if claimed is None:
                return
            try:
                result = execute(claimed)
            except Exception as exc:
                try:
                    self.store.mark_failed(job_id, runner_id, f"{type(exc).__name__}: {exc}")
                except (JobStateError, PermissionError, KeyError):
                    pass
                return
            try:
                self.store.mark_succeeded(job_id, runner_id, result)
            except (JobStateError, PermissionError, KeyError):
                # Another control path may have cancelled/timed out the job.
                pass
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    def resume_queued(
        self,
        execute: JobExecutor,
        *,
        source: str | None = None,
        kind: str | None = None,
    ) -> list[str]:
        started: list[str] = []
        for job in self.store.list_jobs(status=JobStatus.QUEUED, source=source, kind=kind):
            if self.start(job.id, execute):
                started.append(job.id)
        return started

    def wait(self, job_id: str, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._threads.get(job_id)
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()
