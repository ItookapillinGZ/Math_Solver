from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .models import MemoryKind, MemoryRecord, MemoryScope


GLOBAL_SCOPE_ID = "global"
LEGACY_SCOPE_ID = "legacy"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_priority(priority: int) -> int:
    return max(0, min(100, int(priority)))


def _json_dumps(value: dict) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


class MemoryStore:
    """Deterministic SQLite memory store with explicit problem/session scopes."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
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
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_scopes (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    archived_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    key TEXT,
                    content TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 50,
                    scope_id TEXT NOT NULL DEFAULT 'legacy',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (kind IN ('working', 'episodic', 'artifact'))
                );

                CREATE INDEX IF NOT EXISTS idx_memories_kind_priority
                    ON memories(kind, priority DESC, updated_at DESC);

                CREATE TABLE IF NOT EXISTS memory_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            if "scope_id" not in columns:
                conn.execute(
                    "ALTER TABLE memories ADD COLUMN scope_id TEXT NOT NULL DEFAULT 'legacy'"
                )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_scope_kind_priority
                ON memories(scope_id, kind, priority DESC, updated_at DESC)
                """
            )

            # 7C used uniqueness across the whole DB. 7D scopes working/artifact
            # keys so unrelated problems may safely reuse the same key/path.
            conn.executescript(
                """
                DROP INDEX IF EXISTS idx_memories_working_key;
                DROP INDEX IF EXISTS idx_memories_artifact_key;

                CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_working_scope_key
                    ON memories(kind, scope_id, key)
                    WHERE kind = 'working' AND key IS NOT NULL;

                CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_artifact_scope_key
                    ON memories(kind, scope_id, key)
                    WHERE kind = 'artifact' AND key IS NOT NULL;
                """
            )

            now = _utc_now()
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_scopes
                (id, label, archived_at, metadata_json, created_at, updated_at)
                VALUES (?, ?, NULL, '{}', ?, ?)
                """,
                (GLOBAL_SCOPE_ID, "Global promoted memory", now, now),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_scopes
                (id, label, archived_at, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, '{}', ?, ?)
                """,
                (LEGACY_SCOPE_ID, "Legacy pre-scope memory", now, now, now),
            )
        finally:
            conn.close()

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        return MemoryRecord(
            id=row["id"],
            kind=MemoryKind(row["kind"]),
            key=row["key"],
            content=row["content"],
            priority=int(row["priority"]),
            scope_id=row["scope_id"] or LEGACY_SCOPE_ID,
            metadata=metadata if isinstance(metadata, dict) else {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_scope(self, row: sqlite3.Row) -> MemoryScope:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        return MemoryScope(
            id=row["id"],
            label=row["label"],
            archived_at=row["archived_at"],
            metadata=metadata if isinstance(metadata, dict) else {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _ensure_scope(self, conn: sqlite3.Connection, scope_id: str) -> None:
        scope_id = str(scope_id).strip()
        if not scope_id:
            raise ValueError("scope_id must not be empty")
        existing = conn.execute(
            "SELECT 1 FROM memory_scopes WHERE id=?",
            (scope_id,),
        ).fetchone()
        if existing:
            return
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO memory_scopes
            (id, label, archived_at, metadata_json, created_at, updated_at)
            VALUES (?, ?, NULL, '{}', ?, ?)
            """,
            (scope_id, scope_id, now, now),
        )

    # ── Scope lifecycle ──

    def create_scope(
        self,
        label: str,
        *,
        scope_id: str | None = None,
        metadata: dict | None = None,
    ) -> MemoryScope:
        label = str(label).strip() or "Untitled memory scope"
        scope_id = str(scope_id).strip() if scope_id else f"scope_{uuid.uuid4().hex[:16]}"
        if scope_id in {GLOBAL_SCOPE_ID, LEGACY_SCOPE_ID}:
            existing = self.get_scope(scope_id)
            if existing is None:
                raise ValueError(f"reserved scope is missing: {scope_id}")
            return existing
        now = _utc_now()
        with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO memory_scopes
                (id, label, archived_at, metadata_json, created_at, updated_at)
                VALUES (?, ?, NULL, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    label=excluded.label,
                    metadata_json=excluded.metadata_json,
                    archived_at=NULL,
                    updated_at=excluded.updated_at
                """,
                (scope_id, label, _json_dumps(metadata or {}), now, now),
            )
        scope = self.get_scope(scope_id)
        assert scope is not None
        return scope

    def get_scope(self, scope_id: str) -> MemoryScope | None:
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM memory_scopes WHERE id=?",
                (scope_id,),
            ).fetchone()
        return self._row_to_scope(row) if row else None

    def list_scopes(self, *, include_archived: bool = True, limit: int = 100) -> list[MemoryScope]:
        limit = max(1, min(int(limit), 500))
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._read_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memory_scopes
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_scope(row) for row in rows]

    def archive_scope(self, scope_id: str) -> MemoryScope:
        if scope_id in {GLOBAL_SCOPE_ID, LEGACY_SCOPE_ID}:
            raise ValueError(f"reserved scope cannot be archived: {scope_id}")
        now = _utc_now()
        with self._write_transaction() as conn:
            result = conn.execute(
                """
                UPDATE memory_scopes
                SET archived_at=?, updated_at=?
                WHERE id=?
                """,
                (now, now, scope_id),
            )
            if result.rowcount != 1:
                raise KeyError(scope_id)
        scope = self.get_scope(scope_id)
        assert scope is not None
        return scope

    def activate_scope(self, scope_id: str) -> MemoryScope:
        if scope_id in {GLOBAL_SCOPE_ID, LEGACY_SCOPE_ID}:
            raise ValueError(f"reserved scope cannot be activated as a problem scope: {scope_id}")
        now = _utc_now()
        with self._write_transaction() as conn:
            result = conn.execute(
                """
                UPDATE memory_scopes
                SET archived_at=NULL, updated_at=?
                WHERE id=?
                """,
                (now, scope_id),
            )
            if result.rowcount != 1:
                raise KeyError(scope_id)
        scope = self.get_scope(scope_id)
        assert scope is not None
        return scope

    # ── Memory records ──

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def upsert_working(
        self,
        key: str,
        content: str,
        *,
        scope_id: str = GLOBAL_SCOPE_ID,
        priority: int = 70,
        metadata: dict | None = None,
    ) -> MemoryRecord:
        key = str(key).strip()
        content = str(content).strip()
        scope_id = str(scope_id).strip()
        if not key:
            raise ValueError("working memory key must not be empty")
        if not content:
            raise ValueError("working memory content must not be empty")
        if not scope_id:
            raise ValueError("scope_id must not be empty")

        now = _utc_now()
        priority = _normalize_priority(priority)
        with self._write_transaction() as conn:
            self._ensure_scope(conn, scope_id)
            existing = conn.execute(
                """
                SELECT id FROM memories
                WHERE kind='working' AND scope_id=? AND key=?
                """,
                (scope_id, key),
            ).fetchone()
            if existing:
                memory_id = existing["id"]
                conn.execute(
                    """
                    UPDATE memories
                    SET content=?, priority=?, metadata_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (content, priority, _json_dumps(metadata or {}), now, memory_id),
                )
            else:
                memory_id = f"mem_work_{uuid.uuid4().hex[:16]}"
                conn.execute(
                    """
                    INSERT INTO memories
                    (id, kind, key, content, priority, scope_id, metadata_json, created_at, updated_at)
                    VALUES (?, 'working', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        key,
                        content,
                        priority,
                        scope_id,
                        _json_dumps(metadata or {}),
                        now,
                        now,
                    ),
                )
        record = self.get(memory_id)
        assert record is not None
        return record

    def add_episode(
        self,
        content: str,
        *,
        scope_id: str = GLOBAL_SCOPE_ID,
        source: str = "runtime",
        priority: int = 50,
        metadata: dict | None = None,
        memory_id: str | None = None,
    ) -> MemoryRecord:
        content = str(content).strip()
        scope_id = str(scope_id).strip()
        if not content:
            raise ValueError("episodic memory content must not be empty")
        if not scope_id:
            raise ValueError("scope_id must not be empty")
        memory_id = memory_id or f"mem_episode_{uuid.uuid4().hex[:16]}"
        now = _utc_now()
        merged_metadata = dict(metadata or {})
        if source:
            merged_metadata.setdefault("source", source)
        with self._write_transaction() as conn:
            self._ensure_scope(conn, scope_id)
            conn.execute(
                """
                INSERT INTO memories
                (id, kind, key, content, priority, scope_id, metadata_json, created_at, updated_at)
                VALUES (?, 'episodic', NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    content,
                    _normalize_priority(priority),
                    scope_id,
                    _json_dumps(merged_metadata),
                    now,
                    now,
                ),
            )
        record = self.get(memory_id)
        assert record is not None
        return record

    def upsert_artifact(
        self,
        path: str | Path,
        *,
        scope_id: str = GLOBAL_SCOPE_ID,
        description: str = "",
        artifact_type: str = "file",
        priority: int = 60,
        metadata: dict | None = None,
    ) -> MemoryRecord:
        normalized_path = str(Path(path)).replace("\\", "/").strip()
        scope_id = str(scope_id).strip()
        if not normalized_path:
            raise ValueError("artifact path must not be empty")
        if not scope_id:
            raise ValueError("scope_id must not be empty")
        content = description.strip() or f"Artifact: {normalized_path}"
        merged_metadata = dict(metadata or {})
        merged_metadata["path"] = normalized_path
        if artifact_type:
            merged_metadata["artifact_type"] = artifact_type

        now = _utc_now()
        priority = _normalize_priority(priority)
        with self._write_transaction() as conn:
            self._ensure_scope(conn, scope_id)
            existing = conn.execute(
                """
                SELECT id FROM memories
                WHERE kind='artifact' AND scope_id=? AND key=?
                """,
                (scope_id, normalized_path),
            ).fetchone()
            if existing:
                memory_id = existing["id"]
                conn.execute(
                    """
                    UPDATE memories
                    SET content=?, priority=?, metadata_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (content, priority, _json_dumps(merged_metadata), now, memory_id),
                )
            else:
                memory_id = f"mem_artifact_{uuid.uuid4().hex[:16]}"
                conn.execute(
                    """
                    INSERT INTO memories
                    (id, kind, key, content, priority, scope_id, metadata_json, created_at, updated_at)
                    VALUES (?, 'artifact', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        normalized_path,
                        content,
                        priority,
                        scope_id,
                        _json_dumps(merged_metadata),
                        now,
                        now,
                    ),
                )
        record = self.get(memory_id)
        assert record is not None
        return record

    def list_memories(
        self,
        *,
        kind: MemoryKind | str | None = None,
        scope_ids: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        limit = max(1, min(int(limit), 200))
        clauses: list[str] = []
        params: list[object] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(MemoryKind(kind).value)
        if scope_ids is not None:
            normalized = [str(scope).strip() for scope in scope_ids if str(scope).strip()]
            if not normalized:
                return []
            placeholders = ",".join("?" for _ in normalized)
            clauses.append(f"scope_id IN ({placeholders})")
            params.extend(normalized)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._read_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memories
                {where}
                ORDER BY priority DESC, updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def search_text(
        self,
        query: str,
        *,
        kinds: Iterable[MemoryKind | str] | None = None,
        scope_ids: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        query = str(query).strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 200))
        clauses = ["(LOWER(content) LIKE ? OR LOWER(COALESCE(key, '')) LIKE ?)"]
        needle = f"%{query.lower()}%"
        params: list[object] = [needle, needle]

        if kinds:
            kind_values = [MemoryKind(kind).value for kind in kinds]
            placeholders = ",".join("?" for _ in kind_values)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kind_values)

        if scope_ids is not None:
            normalized = [str(scope).strip() for scope in scope_ids if str(scope).strip()]
            if not normalized:
                return []
            placeholders = ",".join("?" for _ in normalized)
            clauses.append(f"scope_id IN ({placeholders})")
            params.extend(normalized)

        params.append(limit)
        with self._read_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memories
                WHERE {' AND '.join(clauses)}
                ORDER BY priority DESC, updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def promote_to_global(self, memory_id: str) -> MemoryRecord:
        record = self.get(memory_id)
        if record is None:
            raise KeyError(memory_id)
        if record.scope_id == GLOBAL_SCOPE_ID:
            return record

        metadata = dict(record.metadata)
        metadata["promoted_from_memory"] = record.id
        metadata["promoted_from_scope"] = record.scope_id

        if record.kind is MemoryKind.WORKING:
            assert record.key is not None
            return self.upsert_working(
                record.key,
                record.content,
                scope_id=GLOBAL_SCOPE_ID,
                priority=record.priority,
                metadata=metadata,
            )
        if record.kind is MemoryKind.ARTIFACT:
            path = record.metadata.get("path") or record.key
            if not path:
                raise ValueError("artifact memory has no path")
            return self.upsert_artifact(
                path,
                scope_id=GLOBAL_SCOPE_ID,
                description=record.content,
                artifact_type=str(record.metadata.get("artifact_type", "file")),
                priority=record.priority,
                metadata=metadata,
            )
        return self.add_episode(
            record.content,
            scope_id=GLOBAL_SCOPE_ID,
            source="memory_promotion",
            priority=record.priority,
            metadata=metadata,
        )

    def build_context(
        self,
        *,
        scope_id: str = GLOBAL_SCOPE_ID,
        include_global: bool = True,
        working_limit: int = 12,
        episodic_limit: int = 6,
        artifact_limit: int = 8,
        max_chars: int = 6000,
    ) -> str:
        scope_id = str(scope_id).strip() or GLOBAL_SCOPE_ID
        sections: list[str] = []

        def append_group(prefix: str, kind: MemoryKind, limit: int, sid: str) -> None:
            records = self.list_memories(kind=kind, scope_ids=[sid], limit=limit)
            if not records:
                return
            if prefix == "Current-scope":
                heading = {
                    MemoryKind.WORKING: "Working memory:",
                    MemoryKind.EPISODIC: "Recent episodic memory:",
                    MemoryKind.ARTIFACT: "Relevant artifacts:",
                }[kind]
            else:
                heading = {
                    MemoryKind.WORKING: "Global promoted working memory:",
                    MemoryKind.EPISODIC: "Global promoted episodic memory:",
                    MemoryKind.ARTIFACT: "Global promoted artifacts:",
                }[kind]
            lines = [heading]
            for record in records:
                if kind is MemoryKind.WORKING:
                    lines.append(f"- {record.key}: {record.content}")
                elif kind is MemoryKind.ARTIFACT:
                    path = record.metadata.get("path") or record.key or record.id
                    lines.append(f"- {path}: {record.content}")
                else:
                    lines.append(f"- {record.content}")
            sections.append("\n".join(lines))

        append_group("Current-scope", MemoryKind.WORKING, working_limit, scope_id)
        append_group("Current-scope", MemoryKind.EPISODIC, episodic_limit, scope_id)
        append_group("Current-scope", MemoryKind.ARTIFACT, artifact_limit, scope_id)

        if include_global and scope_id != GLOBAL_SCOPE_ID:
            append_group("Global promoted", MemoryKind.WORKING, working_limit, GLOBAL_SCOPE_ID)
            append_group("Global promoted", MemoryKind.EPISODIC, episodic_limit, GLOBAL_SCOPE_ID)
            append_group("Global promoted", MemoryKind.ARTIFACT, artifact_limit, GLOBAL_SCOPE_ID)

        text = "\n\n".join(sections)
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 80)].rstrip() + "\n[Memory context truncated by deterministic budget.]"

    # ── Metadata / migrations ──

    def get_meta(self, key: str) -> str | None:
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT value FROM memory_meta WHERE key=?",
                (key,),
            ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO memory_meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    def import_legacy_memory_file(self, path: str | Path) -> bool:
        path = Path(path)
        migration_key = "legacy_memory_md_v1"
        if self.get_meta(migration_key) == "done":
            return False
        if not path.exists():
            self.set_meta(migration_key, "done")
            return False

        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if content:
            self.add_episode(
                content,
                scope_id=LEGACY_SCOPE_ID,
                source="legacy_memory_md",
                priority=40,
                metadata={"legacy_path": str(path)},
            )
        self.set_meta(migration_key, "done")
        return bool(content)
