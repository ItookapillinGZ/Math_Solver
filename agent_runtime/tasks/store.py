from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence

from .models import ClaimResult, TaskRecord


_VALID_STATUSES = {"pending", "in_progress", "completed", "failed", "cancelled"}
_VALID_TASK_TYPES = {"general", "math", "engineering", "literature"}
_DEFAULT_LEASE_SECONDS = 300


class TaskStore:
    """SQLite-backed durable task store with task leases.

    Each public operation opens its own SQLite connection and closes it
    deterministically. Claims use ``BEGIN IMMEDIATE`` so only one worker can
    win a claim race. In-progress tasks may carry a lease; workers renew the
    lease through ``heartbeat_task`` and expired leases can be recovered back
    to ``pending`` after a worker crash.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        default_lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ):
        if default_lease_seconds <= 0:
            raise ValueError("default_lease_seconds must be positive")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_lease_seconds = int(default_lease_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._read_connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'in_progress', 'completed', 'failed', 'cancelled')),
                    owner TEXT,
                    task_type TEXT NOT NULL DEFAULT 'general',
                    required_roles_json TEXT NOT NULL DEFAULT '[]',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    worktree TEXT,
                    lease_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_dependencies (
                    task_id TEXT NOT NULL,
                    depends_on_task_id TEXT NOT NULL,
                    PRIMARY KEY (task_id, depends_on_task_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status
                    ON tasks(status);

                CREATE INDEX IF NOT EXISTS idx_tasks_owner
                    ON tasks(owner);

                CREATE INDEX IF NOT EXISTS idx_tasks_lease_until
                    ON tasks(lease_until);

                CREATE INDEX IF NOT EXISTS idx_task_dependencies_dep
                    ON task_dependencies(depends_on_task_id);
                """
            )
            # Migrate databases created before Step 8F in place. SQLite cannot
            # add several columns in one ALTER TABLE statement, so add only the
            # missing affinity columns individually.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            if "task_type" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'general'"
                )
            if "required_roles_json" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN required_roles_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "tags_json" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_task_type ON tasks(task_type)"
            )

    def _now_dt(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _now(self) -> str:
        return self._now_dt().isoformat()

    def _lease_deadline(self, now: datetime, lease_seconds: int | None) -> str:
        seconds = self.default_lease_seconds if lease_seconds is None else lease_seconds
        if seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        return (now + timedelta(seconds=seconds)).isoformat()

    @staticmethod
    def _lease_is_expired(lease_until: str | None, now: datetime) -> bool:
        if not lease_until:
            return False
        try:
            deadline = datetime.fromisoformat(lease_until)
        except ValueError:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline.astimezone(timezone.utc) <= now

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

    @staticmethod
    def _normalize_task_type(task_type: str | None) -> str:
        value = (task_type or "general").strip().lower()
        if value not in _VALID_TASK_TYPES:
            raise ValueError(
                f"Invalid task_type {task_type!r}; expected one of {sorted(_VALID_TASK_TYPES)}"
            )
        return value

    @staticmethod
    def _normalize_strings(values: Sequence[str] | None) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values or ():
            value = str(raw).strip()
            if value and value.lower() not in seen:
                seen.add(value.lower())
                normalized.append(value)
        return tuple(normalized)

    def create_task(
        self,
        *,
        task_id: str,
        subject: str,
        description: str = "",
        blocked_by: Sequence[str] | None = None,
        worktree: str | None = None,
        task_type: str = "general",
        required_roles: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
    ) -> TaskRecord:
        blocked = tuple(dict.fromkeys(blocked_by or ()))
        task_type = self._normalize_task_type(task_type)
        required_roles = self._normalize_strings(required_roles)
        tags = self._normalize_strings(tags)
        now = self._now()

        with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, subject, description, status, owner, task_type,
                    required_roles_json, tags_json, worktree, lease_until,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', NULL, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    task_id, subject, description, task_type,
                    json.dumps(required_roles, ensure_ascii=False),
                    json.dumps(tags, ensure_ascii=False),
                    worktree, now, now,
                ),
            )
            conn.executemany(
                """
                INSERT INTO task_dependencies (task_id, depends_on_task_id)
                VALUES (?, ?)
                """,
                ((task_id, dep_id) for dep_id in blocked),
            )

        task = self.get_task(task_id)
        assert task is not None
        return task

    def import_task(self, task: TaskRecord, *, overwrite: bool = False) -> bool:
        """Import a task while preserving legacy status/owner/worktree."""
        if task.status not in _VALID_STATUSES:
            raise ValueError(f"Invalid task status: {task.status}")

        created_at = task.created_at or self._now()
        updated_at = task.updated_at or created_at
        blocked = tuple(dict.fromkeys(task.blocked_by))
        task_type = self._normalize_task_type(task.task_type)
        required_roles = self._normalize_strings(task.required_roles)
        tags = self._normalize_strings(task.tags)

        with self._write_transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?",
                (task.id,),
            ).fetchone()
            if exists is not None and not overwrite:
                return False

            if exists is not None:
                conn.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task.id,))
                conn.execute("DELETE FROM tasks WHERE id = ?", (task.id,))

            conn.execute(
                """
                INSERT INTO tasks (
                    id, subject, description, status, owner, task_type,
                    required_roles_json, tags_json, worktree, lease_until,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.subject,
                    task.description,
                    task.status,
                    task.owner,
                    task_type,
                    json.dumps(required_roles, ensure_ascii=False),
                    json.dumps(tags, ensure_ascii=False),
                    task.worktree,
                    task.lease_until,
                    created_at,
                    updated_at,
                ),
            )
            conn.executemany(
                """
                INSERT INTO task_dependencies (task_id, depends_on_task_id)
                VALUES (?, ?)
                """,
                ((task.id, dep_id) for dep_id in blocked),
            )
        return True

    def import_legacy_json_dir(self, directory: str | Path) -> tuple[int, list[str]]:
        """Idempotently migrate legacy .tasks/task_*.json records."""
        directory = Path(directory)
        if not directory.exists():
            return 0, []

        imported = 0
        errors: list[str] = []
        for path in sorted(directory.glob("task_*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                record = TaskRecord(
                    id=str(raw["id"]),
                    subject=str(raw.get("subject", "")),
                    description=str(raw.get("description", "")),
                    status=raw.get("status", "pending"),
                    owner=raw.get("owner"),
                    blocked_by=tuple(raw.get("blockedBy", raw.get("blocked_by", [])) or []),
                    task_type=raw.get("task_type", raw.get("taskType", "general")),
                    required_roles=tuple(raw.get("required_roles", raw.get("requiredRoles", [])) or []),
                    tags=tuple(raw.get("tags", []) or []),
                    worktree=raw.get("worktree"),
                    lease_until=raw.get("lease_until"),
                    created_at=raw.get("created_at"),
                    updated_at=raw.get("updated_at"),
                )
                if self.import_task(record, overwrite=False):
                    imported += 1
            except Exception as exc:
                errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return imported, errors

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_task(conn, row)

    def list_tasks(self) -> list[TaskRecord]:
        with self._read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at, id"
            ).fetchall()
            return [self._row_to_task(conn, row) for row in rows]

    def can_start(self, task_id: str) -> bool:
        with self._read_connection() as conn:
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if task is None or task["status"] != "pending":
                return False
            blocked, missing = self._dependency_problems(conn, task_id)
            return not blocked and not missing

    def claim_task(
        self,
        task_id: str,
        *,
        owner: str = "agent",
        lease_seconds: int | None = None,
    ) -> ClaimResult:
        """Atomically claim a task and attach/renew its worker lease.

        If the task is still marked ``in_progress`` but its lease has expired,
        the same transaction first recovers it to ``pending`` and then allows
        a new worker to claim it. Unleased legacy in-progress tasks are left
        untouched; they are not assumed to be abandoned.
        """
        now = self._now_dt()
        now_text = now.isoformat()
        lease_until = self._lease_deadline(now, lease_seconds)

        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return ClaimResult(False, None, f"Task {task_id} does not exist")

            if row["status"] == "in_progress" and self._lease_is_expired(
                row["lease_until"], now
            ):
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'pending', owner = NULL, lease_until = NULL,
                        updated_at = ?
                    WHERE id = ? AND status = 'in_progress'
                    """,
                    (now_text, task_id),
                )
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()

            if row["status"] != "pending":
                task = self._row_to_task(conn, row)
                return ClaimResult(
                    False,
                    task,
                    f"Task {task_id} is {row['status']}, cannot claim",
                )

            if row["owner"] is not None:
                task = self._row_to_task(conn, row)
                return ClaimResult(
                    False,
                    task,
                    f"Task {task_id} already owned by {row['owner']}",
                )

            blocked, missing = self._dependency_problems(conn, task_id)
            if blocked or missing:
                task = self._row_to_task(conn, row)
                return ClaimResult(
                    False,
                    task,
                    "Task dependencies are not satisfied",
                    blocked_by=tuple(blocked),
                    missing_dependencies=tuple(missing),
                )

            cursor = conn.execute(
                """
                UPDATE tasks
                SET owner = ?, status = 'in_progress', lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'pending' AND owner IS NULL
                """,
                (owner, lease_until, now_text, task_id),
            )

            if cursor.rowcount != 1:
                latest = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                task = self._row_to_task(conn, latest) if latest else None
                return ClaimResult(False, task, f"Task {task_id} lost claim race")

            claimed_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            claimed_task = self._row_to_task(conn, claimed_row)
            return ClaimResult(True, claimed_task, "claimed")

    def heartbeat_task(
        self,
        task_id: str,
        *,
        owner: str,
        lease_seconds: int | None = None,
    ) -> TaskRecord:
        """Renew the lease for a task currently owned by ``owner``.

        Heartbeats are rejected once the old lease has already expired. That
        prevents a stale/crashed worker from reviving a task after another
        worker is allowed to recover it.
        """
        now = self._now_dt()
        now_text = now.isoformat()
        new_lease = self._lease_deadline(now, lease_seconds)

        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Task {task_id} does not exist")
            if row["status"] != "in_progress":
                raise ValueError(
                    f"Task {task_id} is {row['status']}, cannot heartbeat"
                )
            if row["owner"] != owner:
                raise PermissionError(
                    f"Task {task_id} is owned by {row['owner']}, not {owner}"
                )
            if self._lease_is_expired(row["lease_until"], now):
                raise RuntimeError(f"Task {task_id} lease has expired")

            conn.execute(
                """
                UPDATE tasks
                SET lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'in_progress' AND owner = ?
                """,
                (new_lease, now_text, task_id, owner),
            )
            updated = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            return self._row_to_task(conn, updated)

    def recover_expired_tasks(self) -> list[TaskRecord]:
        """Return expired leased tasks to pending so another worker can claim.

        Legacy ``in_progress`` rows with ``lease_until IS NULL`` are preserved
        because there is no reliable evidence that their owner is dead.
        """
        now = self._now_dt()
        now_text = now.isoformat()

        with self._write_transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'in_progress' AND lease_until IS NOT NULL
                ORDER BY id
                """
            ).fetchall()
            expired_ids = [
                row["id"]
                for row in rows
                if self._lease_is_expired(row["lease_until"], now)
            ]
            if not expired_ids:
                return []

            placeholders = ",".join("?" for _ in expired_ids)
            conn.execute(
                f"""
                UPDATE tasks
                SET status = 'pending', owner = NULL, lease_until = NULL,
                    updated_at = ?
                WHERE id IN ({placeholders}) AND status = 'in_progress'
                """,
                (now_text, *expired_ids),
            )
            recovered_rows = conn.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders}) ORDER BY id",
                tuple(expired_ids),
            ).fetchall()
            return [self._row_to_task(conn, row) for row in recovered_rows]

    def complete_task(self, task_id: str, *, owner: str) -> TaskRecord:
        now = self._now_dt()
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Task {task_id} does not exist")
            if row["status"] != "in_progress":
                raise ValueError(
                    f"Task {task_id} is {row['status']}, cannot complete"
                )
            if row["owner"] != owner:
                raise PermissionError(
                    f"Task {task_id} is owned by {row['owner']}, not {owner}"
                )
            if self._lease_is_expired(row["lease_until"], now):
                raise RuntimeError(f"Task {task_id} lease has expired")

            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', lease_until = NULL, updated_at = ?
                WHERE id = ? AND status = 'in_progress' AND owner = ?
                """,
                (now.isoformat(), task_id, owner),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Task {task_id} lost completion race")

            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            return self._row_to_task(conn, row)

    def update_worktree(self, task_id: str, worktree: str | None) -> TaskRecord:
        now = self._now()
        with self._write_transaction() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET worktree = ?, updated_at = ? WHERE id = ?",
                (worktree, now, task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Task {task_id} does not exist")
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            return self._row_to_task(conn, row)

    def set_status(self, task_id: str, status: str) -> TaskRecord:
        if status not in _VALID_STATUSES:
            raise ValueError(f"Invalid task status: {status}")
        now = self._now()
        with self._write_transaction() as conn:
            if status == "in_progress":
                cursor = conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, task_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, lease_until = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, now, task_id),
                )
            if cursor.rowcount != 1:
                raise KeyError(f"Task {task_id} does not exist")
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            return self._row_to_task(conn, row)

    def _dependency_problems(
        self,
        conn: sqlite3.Connection,
        task_id: str,
    ) -> tuple[list[str], list[str]]:
        rows = conn.execute(
            """
            SELECT d.depends_on_task_id AS dependency_id,
                   t.id AS existing_id,
                   t.status AS dependency_status
            FROM task_dependencies AS d
            LEFT JOIN tasks AS t ON t.id = d.depends_on_task_id
            WHERE d.task_id = ?
            ORDER BY d.depends_on_task_id
            """,
            (task_id,),
        ).fetchall()

        blocked: list[str] = []
        missing: list[str] = []
        for row in rows:
            if row["existing_id"] is None:
                missing.append(row["dependency_id"])
            elif row["dependency_status"] != "completed":
                blocked.append(row["dependency_id"])
        return blocked, missing

    def _row_to_task(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> TaskRecord:
        deps = conn.execute(
            """
            SELECT depends_on_task_id
            FROM task_dependencies
            WHERE task_id = ?
            ORDER BY depends_on_task_id
            """,
            (row["id"],),
        ).fetchall()
        blocked_by = tuple(dep["depends_on_task_id"] for dep in deps)
        return TaskRecord(
            id=row["id"],
            subject=row["subject"],
            description=row["description"],
            status=row["status"],
            owner=row["owner"],
            blocked_by=blocked_by,
            task_type=row["task_type"] if "task_type" in row.keys() else "general",
            required_roles=tuple(
                json.loads(row["required_roles_json"] or "[]")
                if "required_roles_json" in row.keys() else []
            ),
            tags=tuple(
                json.loads(row["tags_json"] or "[]")
                if "tags_json" in row.keys() else []
            ),
            worktree=row["worktree"],
            lease_until=row["lease_until"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
