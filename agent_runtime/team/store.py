from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class MessageRecord:
    id: int
    from_agent: str
    to_agent: str
    content: str
    msg_type: str
    ts: float
    metadata: dict[str, Any]
    consumed_at: float | None = None

    def to_message(self) -> dict[str, Any]:
        """Return the legacy MessageBus dictionary contract."""
        return {
            "from": self.from_agent,
            "to": self.to_agent,
            "content": self.content,
            "type": self.msg_type,
            "ts": self.ts,
            "metadata": dict(self.metadata),
        }


class MessageStore:
    """SQLite-backed durable storage for inter-agent messages.

    ``consume_inbox`` preserves the historical MessageBus contract: reading an
    inbox consumes its currently unread messages. The select + consume update
    happens inside one ``BEGIN IMMEDIATE`` transaction so concurrent readers do
    not receive the same message batch.
    """

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=max(self.busy_timeout_ms / 1000.0, 0.001),
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_agent TEXT NOT NULL,
                    to_agent TEXT NOT NULL,
                    content TEXT NOT NULL,
                    msg_type TEXT NOT NULL,
                    ts REAL NOT NULL,
                    metadata_json TEXT NOT NULL,
                    consumed_at REAL,
                    import_key TEXT UNIQUE
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_inbox
                ON messages(to_agent, consumed_at, id)
                """
            )
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def _metadata_json(metadata: dict[str, Any] | None) -> str:
        payload = metadata if isinstance(metadata, dict) else {}
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _decode_metadata(raw: str) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _row_to_record(self, row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            id=int(row["id"]),
            from_agent=str(row["from_agent"]),
            to_agent=str(row["to_agent"]),
            content=str(row["content"]),
            msg_type=str(row["msg_type"]),
            ts=float(row["ts"]),
            metadata=self._decode_metadata(row["metadata_json"]),
            consumed_at=(
                float(row["consumed_at"])
                if row["consumed_at"] is not None
                else None
            ),
        )

    def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        msg_type: str = "message",
        metadata: dict[str, Any] | None = None,
        *,
        ts: float | None = None,
        import_key: str | None = None,
    ) -> MessageRecord:
        timestamp = time.time() if ts is None else float(ts)
        with self._write_transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages (
                    from_agent, to_agent, content, msg_type, ts,
                    metadata_json, consumed_at, import_key
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    str(from_agent),
                    str(to_agent),
                    str(content),
                    str(msg_type or "message"),
                    timestamp,
                    self._metadata_json(metadata),
                    import_key,
                ),
            )
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            assert row is not None
            return self._row_to_record(row)

    def consume_inbox(
        self,
        agent: str,
        *,
        limit: int | None = None,
    ) -> list[MessageRecord]:
        """Atomically fetch and consume unread messages in FIFO order.

        ``limit=None`` preserves the legacy mailbox behavior of draining the
        whole inbox in one read. A bounded limit is available for future callers.
        """
        consumed_at = time.time()
        with self._write_transaction() as conn:
            if limit is None:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE to_agent = ? AND consumed_at IS NULL
                    ORDER BY id
                    """,
                    (str(agent),),
                ).fetchall()
            else:
                bounded_limit = max(1, min(int(limit), 10_000))
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE to_agent = ? AND consumed_at IS NULL
                    ORDER BY id
                    LIMIT ?
                    """,
                    (str(agent), bounded_limit),
                ).fetchall()
            if not rows:
                return []
            ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE messages
                SET consumed_at = ?
                WHERE consumed_at IS NULL AND id IN ({placeholders})
                """,
                (consumed_at, *ids),
            )
            # We still hold the write transaction. No other consumer can have
            # claimed these rows between SELECT and UPDATE.
            return [self._row_to_record(row) for row in rows]

    def unread_count(self, agent: str | None = None) -> int:
        conn = self._connect()
        try:
            if agent is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE consumed_at IS NULL"
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM messages
                    WHERE to_agent = ? AND consumed_at IS NULL
                    """,
                    (str(agent),),
                ).fetchone()
            assert row is not None
            return int(row["n"])
        finally:
            conn.close()

    def total_count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
            assert row is not None
            return int(row["n"])
        finally:
            conn.close()

    def migrate_legacy_mailboxes(self, mailbox_dir: Path) -> dict[str, int]:
        """Import legacy ``*.jsonl`` inboxes without duplicating messages.

        Each imported line receives a deterministic ``import_key``. If a crash
        occurs after the SQLite commit but before the JSONL file is removed,
        re-running migration safely ignores the already imported lines.
        Malformed files are kept on disk for manual inspection.
        """
        directory = Path(mailbox_dir)
        if not directory.exists():
            return {"imported": 0, "duplicates": 0, "files_removed": 0, "errors": 0}

        imported = 0
        duplicates = 0
        files_removed = 0
        errors = 0

        for path in sorted(directory.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                errors += 1
                continue

            parsed: list[tuple[dict[str, Any], str]] = []
            file_has_error = False
            for line_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    file_has_error = True
                    errors += 1
                    continue
                if not isinstance(value, dict):
                    file_has_error = True
                    errors += 1
                    continue
                digest = hashlib.sha256(line.encode("utf-8")).hexdigest()
                key = f"legacy:{path.name}:{line_number}:{digest}"
                parsed.append((value, key))

            for msg, import_key in parsed:
                try:
                    with self._write_transaction() as conn:
                        cursor = conn.execute(
                            """
                            INSERT OR IGNORE INTO messages (
                                from_agent, to_agent, content, msg_type, ts,
                                metadata_json, consumed_at, import_key
                            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                            """,
                            (
                                str(msg.get("from", "unknown")),
                                str(msg.get("to", path.stem)),
                                str(msg.get("content", "")),
                                str(msg.get("type", "message") or "message"),
                                float(msg.get("ts", time.time())),
                                self._metadata_json(msg.get("metadata")),
                                import_key,
                            ),
                        )
                        if cursor.rowcount == 1:
                            imported += 1
                        else:
                            duplicates += 1
                except Exception:
                    file_has_error = True
                    errors += 1

            if not file_has_error:
                try:
                    path.unlink(missing_ok=True)
                    files_removed += 1
                except Exception:
                    errors += 1

        return {
            "imported": imported,
            "duplicates": duplicates,
            "files_removed": files_removed,
            "errors": errors,
        }
