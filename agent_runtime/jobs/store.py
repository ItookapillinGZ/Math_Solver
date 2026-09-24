from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import CronScheduleRecord, JobRecord, JobStatus


class JobStateError(RuntimeError):
    """Raised when a job transition is invalid for its current state."""


class JobStore:
    """Durable SQLite-backed job queue with atomic claiming and state transitions."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._write_transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    source TEXT,
                    schedule_id TEXT,
                    runner_id TEXT,
                    timeout_seconds REAL,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    notified_at TEXT,
                    CHECK (status IN (
                        'queued', 'running', 'succeeded',
                        'failed', 'cancelled', 'timed_out'
                    )),
                    CHECK (max_attempts >= 1),
                    CHECK (attempt_count >= 0)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status_available
                    ON jobs(status, available_at, created_at);

                CREATE INDEX IF NOT EXISTS idx_jobs_schedule_id
                    ON jobs(schedule_id);

                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    cron TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    recurring INTEGER NOT NULL,
                    durable INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    session_id TEXT,
                    last_fired_marker TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    cancelled_at TEXT,
                    CHECK (recurring IN (0, 1)),
                    CHECK (durable IN (0, 1)),
                    CHECK (active IN (0, 1))
                );

                CREATE INDEX IF NOT EXISTS idx_schedules_active
                    ON schedules(active, durable, session_id, created_at);
                """
            )
            # 6B migration: databases created by 6A do not yet have this column.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "notified_at" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN notified_at TEXT")

    @staticmethod
    def _decode_json(value: str | None) -> Any | None:
        if value is None:
            return None
        return json.loads(value)

    @staticmethod
    def _encode_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _row_to_job(self, row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            kind=row["kind"],
            status=JobStatus(row["status"]),
            payload=self._decode_json(row["payload_json"]),
            result=self._decode_json(row["result_json"]),
            error=row["error"],
            source=row["source"],
            schedule_id=row["schedule_id"],
            runner_id=row["runner_id"],
            timeout_seconds=row["timeout_seconds"],
            max_attempts=row["max_attempts"],
            attempt_count=row["attempt_count"],
            available_at=row["available_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            notified_at=row["notified_at"],
        )

    def _row_to_schedule(self, row: sqlite3.Row) -> CronScheduleRecord:
        return CronScheduleRecord(
            id=row["id"],
            cron=row["cron"],
            prompt=row["prompt"],
            recurring=bool(row["recurring"]),
            durable=bool(row["durable"]),
            active=bool(row["active"]),
            session_id=row["session_id"],
            last_fired_marker=row["last_fired_marker"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            cancelled_at=row["cancelled_at"],
        )

    def create_schedule(
        self,
        cron: str,
        prompt: str,
        *,
        recurring: bool = True,
        durable: bool = True,
        schedule_id: str | None = None,
        session_id: str | None = None,
    ) -> CronScheduleRecord:
        if not cron.strip():
            raise ValueError("cron must not be empty")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if not durable and not (session_id or "").strip():
            raise ValueError("session_id is required for non-durable schedules")

        now_text = self._iso(self._now())
        schedule_id = schedule_id or f"cron_{uuid.uuid4().hex[:12]}"
        stored_session = None if durable else session_id
        with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO schedules (
                    id, cron, prompt, recurring, durable, active, session_id,
                    last_fired_marker, created_at, updated_at, cancelled_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, NULL, ?, ?, NULL)
                """,
                (
                    schedule_id, cron, prompt, int(recurring), int(durable),
                    stored_session, now_text, now_text,
                ),
            )
            row = conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            return self._row_to_schedule(row)

    def create_schedule_once(
        self,
        cron: str,
        prompt: str,
        *,
        schedule_id: str,
        recurring: bool = True,
        durable: bool = True,
        session_id: str | None = None,
    ) -> tuple[CronScheduleRecord, bool]:
        if not schedule_id.strip():
            raise ValueError("schedule_id must not be empty")
        if not cron.strip():
            raise ValueError("cron must not be empty")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if not durable and not (session_id or "").strip():
            raise ValueError("session_id is required for non-durable schedules")

        now_text = self._iso(self._now())
        stored_session = None if durable else session_id
        with self._write_transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO schedules (
                    id, cron, prompt, recurring, durable, active, session_id,
                    last_fired_marker, created_at, updated_at, cancelled_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, NULL, ?, ?, NULL)
                """,
                (
                    schedule_id, cron, prompt, int(recurring), int(durable),
                    stored_session, now_text, now_text,
                ),
            )
            row = conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Failed to create or load schedule {schedule_id}")
            created = cursor.rowcount == 1
            if not created:
                if (
                    row["cron"] != cron
                    or row["prompt"] != prompt
                    or bool(row["recurring"]) != bool(recurring)
                    or bool(row["durable"]) != bool(durable)
                ):
                    raise JobStateError(
                        f"Schedule idempotency key collision for {schedule_id}"
                    )
            return self._row_to_schedule(row), created

    def get_schedule(self, schedule_id: str) -> CronScheduleRecord:
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Schedule {schedule_id} does not exist")
            return self._row_to_schedule(row)

    def list_schedules(
        self,
        *,
        active_only: bool = False,
        session_id: str | None = None,
    ) -> list[CronScheduleRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("active = 1")
        if session_id is not None:
            clauses.append("(durable = 1 OR session_id = ?)")
            params.append(session_id)
        query = "SELECT * FROM schedules"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, id"
        with self._read_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_schedule(row) for row in rows]

    def mark_schedule_fired(
        self, schedule_id: str, marker: str
    ) -> CronScheduleRecord:
        if not marker.strip():
            raise ValueError("marker must not be empty")
        now_text = self._iso(self._now())
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Schedule {schedule_id} does not exist")
            if not bool(row["active"]):
                return self._row_to_schedule(row)
            new_active = 1 if bool(row["recurring"]) else 0
            conn.execute(
                """
                UPDATE schedules
                SET last_fired_marker = ?, active = ?, updated_at = ?
                WHERE id = ?
                """,
                (marker, new_active, now_text, schedule_id),
            )
            updated = conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            return self._row_to_schedule(updated)

    def deactivate_schedule(self, schedule_id: str) -> CronScheduleRecord:
        now_text = self._iso(self._now())
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Schedule {schedule_id} does not exist")
            conn.execute(
                "UPDATE schedules SET active = 0, updated_at = ? WHERE id = ?",
                (now_text, schedule_id),
            )
            updated = conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            return self._row_to_schedule(updated)

    def cancel_schedule(self, schedule_id: str) -> CronScheduleRecord:
        now_text = self._iso(self._now())
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Schedule {schedule_id} does not exist")
            if not bool(row["active"]):
                return self._row_to_schedule(row)
            conn.execute(
                """
                UPDATE schedules
                SET active = 0, cancelled_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_text, now_text, schedule_id),
            )
            updated = conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            return self._row_to_schedule(updated)

    def create_job(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        job_id: str | None = None,
        source: str | None = None,
        schedule_id: str | None = None,
        timeout_seconds: float | None = None,
        max_attempts: int = 1,
        available_at: datetime | None = None,
    ) -> JobRecord:
        if not kind.strip():
            raise ValueError("kind must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")

        now = self._now()
        now_text = self._iso(now)
        job_id = job_id or f"job_{uuid.uuid4().hex[:12]}"
        available_text = self._iso(available_at) if available_at else None

        with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, kind, status, payload_json, result_json, error,
                    source, schedule_id, runner_id, timeout_seconds,
                    max_attempts, attempt_count, available_at,
                    created_at, updated_at, started_at, finished_at, notified_at
                ) VALUES (?, ?, 'queued', ?, NULL, NULL, ?, ?, NULL, ?, ?, 0, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    job_id,
                    kind,
                    self._encode_json(payload),
                    source,
                    schedule_id,
                    timeout_seconds,
                    max_attempts,
                    available_text,
                    now_text,
                    now_text,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row)

    def create_job_once(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        job_id: str,
        source: str | None = None,
        schedule_id: str | None = None,
        timeout_seconds: float | None = None,
        max_attempts: int = 1,
        available_at: datetime | None = None,
    ) -> tuple[JobRecord, bool]:
        """Create a deterministic job exactly once.

        Returns ``(job, created)``. Repeating the same logical submission with
        the same ``job_id`` returns the existing record instead of raising a
        uniqueness error. This is used by cron occurrence dispatch so a process
        restart within the same minute does not enqueue the same firing twice.
        """
        if not job_id.strip():
            raise ValueError("job_id must not be empty")
        if not kind.strip():
            raise ValueError("kind must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")

        now = self._now()
        now_text = self._iso(now)
        available_text = self._iso(available_at) if available_at else None

        with self._write_transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    id, kind, status, payload_json, result_json, error,
                    source, schedule_id, runner_id, timeout_seconds,
                    max_attempts, attempt_count, available_at,
                    created_at, updated_at, started_at, finished_at, notified_at
                ) VALUES (?, ?, 'queued', ?, NULL, NULL, ?, ?, NULL, ?, ?, 0, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    job_id,
                    kind,
                    self._encode_json(payload),
                    source,
                    schedule_id,
                    timeout_seconds,
                    max_attempts,
                    available_text,
                    now_text,
                    now_text,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"Failed to create or load job {job_id}")

            created = cursor.rowcount == 1
            if not created:
                # Deterministic ids are an idempotency key. A collision with a
                # different logical job is a programming error, not a harmless
                # retry, so reject it explicitly.
                if (
                    row["kind"] != kind
                    or row["source"] != source
                    or row["schedule_id"] != schedule_id
                ):
                    raise JobStateError(
                        f"Job idempotency key collision for {job_id}"
                    )
            return self._row_to_job(row), created

    def get_job(self, job_id: str) -> JobRecord:
        with self._read_connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            return self._row_to_job(row)

    def list_jobs(
        self,
        *,
        status: JobStatus | str | None = None,
        source: str | None = None,
        kind: str | None = None,
        schedule_id: str | None = None,
    ) -> list[JobRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(JobStatus(status).value)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if schedule_id is not None:
            clauses.append("schedule_id = ?")
            params.append(schedule_id)
        query = "SELECT * FROM jobs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, id"
        with self._read_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_job(row) for row in rows]

    def list_unnotified_terminal(
        self,
        *,
        source: str | None = None,
        kind: str | None = None,
    ) -> list[JobRecord]:
        clauses = [
            "status IN ('succeeded','failed','cancelled','timed_out')",
            "notified_at IS NULL",
        ]
        params: list[Any] = []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        query = (
            "SELECT * FROM jobs WHERE " + " AND ".join(clauses)
            + " ORDER BY COALESCE(finished_at, updated_at), created_at, id"
        )
        with self._read_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_job(row) for row in rows]

    def mark_notified(self, job_id: str) -> JobRecord:
        now_text = self._iso(self._now())
        with self._write_transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            if not JobStatus(row["status"]).is_terminal:
                raise JobStateError(f"Job {job_id} is not terminal")
            conn.execute(
                "UPDATE jobs SET notified_at = COALESCE(notified_at, ?), updated_at = ? WHERE id = ?",
                (now_text, now_text, job_id),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(updated)

    def claim_job(self, job_id: str, runner_id: str) -> JobRecord | None:
        """Atomically claim one specific runnable job."""
        if not runner_id.strip():
            raise ValueError("runner_id must not be empty")
        now_text = self._iso(self._now())
        with self._write_transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            if row["status"] != JobStatus.QUEUED.value:
                return None
            if row["available_at"] is not None and row["available_at"] > now_text:
                return None
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'running', runner_id = ?,
                    attempt_count = attempt_count + 1,
                    started_at = ?, finished_at = NULL,
                    error = NULL, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (runner_id, now_text, now_text, job_id),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(claimed)

    def claim_next(
        self,
        runner_id: str,
        *,
        kinds: Iterable[str] | None = None,
    ) -> JobRecord | None:
        """Atomically claim one runnable queued job for ``runner_id``."""
        if not runner_id.strip():
            raise ValueError("runner_id must not be empty")

        now = self._now()
        now_text = self._iso(now)
        kinds_tuple = tuple(dict.fromkeys(kinds or ()))

        with self._write_transaction() as conn:
            params: list[Any] = [now_text]
            query = (
                "SELECT * FROM jobs "
                "WHERE status = 'queued' "
                "AND (available_at IS NULL OR available_at <= ?)"
            )
            if kinds_tuple:
                placeholders = ",".join("?" for _ in kinds_tuple)
                query += f" AND kind IN ({placeholders})"
                params.extend(kinds_tuple)
            query += " ORDER BY created_at, id LIMIT 1"

            row = conn.execute(query, params).fetchone()
            if row is None:
                return None

            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'running', runner_id = ?,
                    attempt_count = attempt_count + 1,
                    started_at = ?, finished_at = NULL,
                    error = NULL, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (runner_id, now_text, now_text, row["id"]),
            )
            if cursor.rowcount != 1:
                return None

            claimed = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            return self._row_to_job(claimed)

    def _require_running_owner(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        runner_id: str,
    ) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Job {job_id} does not exist")
        if row["status"] != JobStatus.RUNNING.value:
            raise JobStateError(
                f"Job {job_id} is {row['status']}, expected running"
            )
        if row["runner_id"] != runner_id:
            raise PermissionError(
                f"Job {job_id} is owned by runner {row['runner_id']}, not {runner_id}"
            )
        return row

    def mark_succeeded(self, job_id: str, runner_id: str, result: Any = None) -> JobRecord:
        now_text = self._iso(self._now())
        with self._write_transaction() as conn:
            self._require_running_owner(conn, job_id, runner_id)
            conn.execute(
                """
                UPDATE jobs
                SET status = 'succeeded', result_json = ?, error = NULL,
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (self._encode_json(result), now_text, now_text, job_id),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row)

    def mark_failed(
        self,
        job_id: str,
        runner_id: str,
        error: str,
        *,
        retry: bool = False,
    ) -> JobRecord:
        now_text = self._iso(self._now())
        with self._write_transaction() as conn:
            row = self._require_running_owner(conn, job_id, runner_id)
            may_retry = retry and row["attempt_count"] < row["max_attempts"]
            if may_retry:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued', runner_id = NULL,
                        error = ?, started_at = NULL, finished_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (error, now_text, job_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', error = ?,
                        finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (error, now_text, now_text, job_id),
                )
            updated = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._row_to_job(updated)

    def mark_timed_out(self, job_id: str, runner_id: str, error: str | None = None) -> JobRecord:
        now_text = self._iso(self._now())
        with self._write_transaction() as conn:
            self._require_running_owner(conn, job_id, runner_id)
            conn.execute(
                """
                UPDATE jobs
                SET status = 'timed_out', error = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (error or "job timed out", now_text, now_text, job_id),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row)

    def cancel_job(self, job_id: str) -> JobRecord:
        """Persist cancellation intent/state.

        6A only stores the state transition. A future runner integration will
        cooperatively stop the underlying thread/process when possible.
        """
        now_text = self._iso(self._now())
        with self._write_transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            status = JobStatus(row["status"])
            if status.is_terminal:
                return self._row_to_job(row)
            conn.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_text, now_text, job_id),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(updated)

    def recover_interrupted_jobs(
        self,
        *,
        source: str | None = None,
        kind: str | None = None,
    ) -> list[JobRecord]:
        """Recover RUNNING jobs left behind after a process crash/restart."""
        now_text = self._iso(self._now())
        clauses = ["status = 'running'"]
        params: list[Any] = []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        query = "SELECT * FROM jobs WHERE " + " AND ".join(clauses) + " ORDER BY created_at, id"
        recovered_ids: list[str] = []
        with self._write_transaction() as conn:
            rows = conn.execute(query, params).fetchall()
            for row in rows:
                if row["attempt_count"] < row["max_attempts"]:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'queued', runner_id = NULL,
                            error = 'interrupted by process restart',
                            started_at = NULL, finished_at = NULL,
                            updated_at = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (now_text, row["id"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed',
                            error = 'interrupted by process restart; retry budget exhausted',
                            finished_at = ?, updated_at = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (now_text, now_text, row["id"]),
                    )
                recovered_ids.append(row["id"])

            return [
                self._row_to_job(
                    conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                )
                for job_id in recovered_ids
            ]

