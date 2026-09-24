from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class TracePricing:
    """Optional model pricing used only for local cost estimation.

    Values are USD per million tokens.  The runtime deliberately does not
    hard-code vendor prices because they change independently of this project.
    Unconfigured models simply store ``estimated_cost_usd = NULL``.
    """

    input_per_mtok: Mapping[str, float] = field(default_factory=dict)
    output_per_mtok: Mapping[str, float] = field(default_factory=dict)
    cache_write_per_mtok: Mapping[str, float] = field(default_factory=dict)
    cache_read_per_mtok: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: str | None) -> "TracePricing":
        if not raw or not str(raw).strip():
            return cls()
        try:
            data = json.loads(raw)
        except Exception:
            return cls()
        if not isinstance(data, dict):
            return cls()
        inputs: dict[str, float] = {}
        outputs: dict[str, float] = {}
        writes: dict[str, float] = {}
        reads: dict[str, float] = {}
        for model, prices in data.items():
            if not isinstance(prices, dict):
                continue
            try:
                if "input" in prices:
                    inputs[str(model)] = float(prices["input"])
                if "output" in prices:
                    outputs[str(model)] = float(prices["output"])
                if "cache_write" in prices:
                    writes[str(model)] = float(prices["cache_write"])
                if "cache_read" in prices:
                    reads[str(model)] = float(prices["cache_read"])
            except (TypeError, ValueError):
                continue
        return cls(inputs, outputs, writes, reads)

    def estimate(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> float | None:
        if model not in self.input_per_mtok or model not in self.output_per_mtok:
            return None
        total = (
            input_tokens * float(self.input_per_mtok[model])
            + output_tokens * float(self.output_per_mtok[model])
            + cache_creation_input_tokens
            * float(self.cache_write_per_mtok.get(model, 0.0))
            + cache_read_input_tokens
            * float(self.cache_read_per_mtok.get(model, 0.0))
        ) / 1_000_000.0
        return round(total, 8)


class TraceStore:
    """SQLite persistence for runtime, workflow, policy, and task observability.

    Raw prompts, raw tool inputs, and full tool outputs are intentionally not
    stored here.  The store receives the safe metadata already emitted by the
    runtime plus compact workflow snapshots required for crash recovery.
    """

    def __init__(self, db_path: Path, *, pricing: TracePricing | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.pricing = pricing or TracePricing()
        self._lock = threading.RLock()
        self._initialize()

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    agent_id TEXT,
                    tool_call_id TEXT,
                    task_id TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_events_run
                    ON runtime_events(run_id, ts);
                CREATE INDEX IF NOT EXISTS idx_runtime_events_type
                    ON runtime_events(event_type, ts);

                CREATE TABLE IF NOT EXISTS model_calls (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    agent_id TEXT,
                    ts TEXT NOT NULL,
                    model TEXT,
                    latency_ms REAL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    stop_reason TEXT,
                    estimated_cost_usd REAL
                );
                CREATE INDEX IF NOT EXISTS idx_model_calls_run
                    ON model_calls(run_id, ts);

                CREATE TABLE IF NOT EXISTS policy_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    runtime_run_id TEXT,
                    agent_id TEXT,
                    tool_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT,
                    user_approved INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_policy_run
                    ON policy_decisions(runtime_run_id, ts);

                CREATE TABLE IF NOT EXISTS task_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    runtime_run_id TEXT,
                    task_id TEXT NOT NULL,
                    transition TEXT NOT NULL,
                    status TEXT,
                    owner TEXT,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_transitions_task
                    ON task_transitions(task_id, ts);

                CREATE TABLE IF NOT EXISTS workflow_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    research_run_id TEXT NOT NULL,
                    runtime_run_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_events_run
                    ON workflow_events(research_run_id, ts);

                CREATE TABLE IF NOT EXISTS workflow_snapshots (
                    research_run_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    run_json TEXT NOT NULL,
                    bindings_json TEXT NOT NULL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _event_dict(event: Any) -> dict[str, Any]:
        if hasattr(event, "to_dict"):
            return dict(event.to_dict())
        if isinstance(event, Mapping):
            return dict(event)
        raise TypeError("runtime event must provide to_dict() or Mapping")

    def record_runtime_event(self, event: Any) -> None:
        data = self._event_dict(event)
        payload = dict(data.get("payload") or {})
        event_type = str(data.get("type") or data.get("event_type") or "")
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO runtime_events
                (id,event_type,ts,run_id,agent_id,tool_call_id,task_id,payload_json)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    str(data.get("id", "")),
                    event_type,
                    str(data.get("timestamp") or data.get("ts") or self._utc_now()),
                    str(data.get("run_id", "")),
                    data.get("agent_id"),
                    data.get("tool_call_id"),
                    data.get("task_id"),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
            if event_type == "model_call_finished":
                model = str(payload.get("model") or "")
                input_tokens = int(payload.get("input_tokens") or 0)
                output_tokens = int(payload.get("output_tokens") or 0)
                cache_creation = int(payload.get("cache_creation_input_tokens") or 0)
                cache_read = int(payload.get("cache_read_input_tokens") or 0)
                cost = self.pricing.estimate(
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_creation_input_tokens=cache_creation,
                    cache_read_input_tokens=cache_read,
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO model_calls
                    (event_id,run_id,agent_id,ts,model,latency_ms,input_tokens,
                     output_tokens,cache_creation_input_tokens,cache_read_input_tokens,
                     retry_count,stop_reason,estimated_cost_usd)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(data.get("id", "")),
                        str(data.get("run_id", "")),
                        data.get("agent_id"),
                        str(data.get("timestamp") or self._utc_now()),
                        model or None,
                        float(payload.get("latency_ms") or 0.0),
                        input_tokens,
                        output_tokens,
                        cache_creation,
                        cache_read,
                        int(payload.get("retry_count") or 0),
                        str(payload.get("stop_reason") or "") or None,
                        cost,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def record_policy_decision(self, payload: Mapping[str, Any]) -> None:
        conn = self._connect()
        try:
            approved = payload.get("user_approved")
            conn.execute(
                """
                INSERT INTO policy_decisions
                (ts,runtime_run_id,agent_id,tool_name,action,reason,user_approved)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    str(payload.get("ts") or self._utc_now()),
                    payload.get("runtime_run_id"),
                    payload.get("agent_id"),
                    str(payload.get("tool_name") or ""),
                    str(payload.get("action") or ""),
                    str(payload.get("reason") or "") or None,
                    None if approved is None else int(bool(approved)),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def record_task_transition(self, payload: Mapping[str, Any]) -> None:
        metadata = dict(payload.get("metadata") or {})
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO task_transitions
                (ts,runtime_run_id,task_id,transition,status,owner,metadata_json)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    str(payload.get("ts") or self._utc_now()),
                    payload.get("runtime_run_id"),
                    str(payload.get("task_id") or ""),
                    str(payload.get("transition") or ""),
                    payload.get("status"),
                    payload.get("owner"),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def record_workflow_event(
        self,
        event_type: str,
        research_run_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        runtime_run_id: str | None = None,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO workflow_events
                (ts,research_run_id,runtime_run_id,event_type,payload_json)
                VALUES (?,?,?,?,?)
                """,
                (
                    self._utc_now(),
                    str(research_run_id),
                    runtime_run_id,
                    str(event_type),
                    json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def save_workflow_snapshot(
        self,
        research_run_id: str,
        run_data: Mapping[str, Any],
        bindings_data: list[Mapping[str, Any]],
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO workflow_snapshots
                (research_run_id,updated_at,run_json,bindings_json)
                VALUES (?,?,?,?)
                ON CONFLICT(research_run_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    run_json=excluded.run_json,
                    bindings_json=excluded.bindings_json
                """,
                (
                    str(research_run_id),
                    self._utc_now(),
                    json.dumps(dict(run_data), ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps([dict(item) for item in bindings_data], ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def load_workflow_snapshots(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT research_run_id,updated_at,run_json,bindings_json "
                "FROM workflow_snapshots ORDER BY updated_at"
            ).fetchall()
            return [
                {
                    "research_run_id": row["research_run_id"],
                    "updated_at": row["updated_at"],
                    "run": json.loads(row["run_json"]),
                    "bindings": json.loads(row["bindings_json"]),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def list_runtime_events(self, run_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM runtime_events WHERE run_id=? ORDER BY ts,id",
                (run_id,),
            ).fetchall()
            return [self._runtime_row(row) for row in rows]
        finally:
            conn.close()

    def list_workflow_events(self, research_run_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM workflow_events WHERE research_run_id=? ORDER BY ts,id",
                (research_run_id,),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "research_run_id": row["research_run_id"],
                    "runtime_run_id": row["runtime_run_id"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def list_recent_runtime_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT run_id, MAX(ts) AS last_ts
                FROM runtime_events
                GROUP BY run_id
                ORDER BY last_ts DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [self.runtime_run_summary(row["run_id"]) for row in rows]
        finally:
            conn.close()

    def runtime_run_summary(self, run_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            events = conn.execute(
                "SELECT event_type,ts,payload_json FROM runtime_events "
                "WHERE run_id=? ORDER BY ts,id",
                (run_id,),
            ).fetchall()
            if not events:
                raise KeyError(run_id)
            agent_name = None
            agent_kind = None
            role = None
            status = "running"
            started_at = events[0]["ts"]
            ended_at = None
            tool_calls = 0
            blocked_tools = 0
            for row in events:
                payload = json.loads(row["payload_json"])
                if row["event_type"] == "agent_started":
                    agent_name = payload.get("name")
                    agent_kind = payload.get("kind")
                    role = payload.get("role")
                elif row["event_type"] == "tool_call_finished":
                    tool_calls += 1
                    if payload.get("status") == "blocked":
                        blocked_tools += 1
                elif row["event_type"] == "run_finished":
                    status = payload.get("status") or status
                    ended_at = row["ts"]

            usage = conn.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(latency_ms),0) AS latency_ms,
                       COALESCE(SUM(input_tokens),0) AS input_tokens,
                       COALESCE(SUM(output_tokens),0) AS output_tokens,
                       COALESCE(SUM(cache_creation_input_tokens),0) AS cache_creation,
                       COALESCE(SUM(cache_read_input_tokens),0) AS cache_read,
                       COALESCE(SUM(retry_count),0) AS retries,
                       SUM(estimated_cost_usd) AS cost
                FROM model_calls WHERE run_id=?
                """,
                (run_id,),
            ).fetchone()
            permissions = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN action='deny' THEN 1 ELSE 0 END) AS denied, "
                "SUM(CASE WHEN action='ask' THEN 1 ELSE 0 END) AS asked "
                "FROM policy_decisions WHERE runtime_run_id=?",
                (run_id,),
            ).fetchone()
            return {
                "run_id": run_id,
                "agent_name": agent_name,
                "agent_kind": agent_kind,
                "role": role,
                "status": status,
                "started_at": started_at,
                "ended_at": ended_at,
                "model_calls": int(usage["calls"] or 0),
                "model_latency_ms": float(usage["latency_ms"] or 0.0),
                "input_tokens": int(usage["input_tokens"] or 0),
                "output_tokens": int(usage["output_tokens"] or 0),
                "cache_creation_input_tokens": int(usage["cache_creation"] or 0),
                "cache_read_input_tokens": int(usage["cache_read"] or 0),
                "retry_count": int(usage["retries"] or 0),
                "estimated_cost_usd": usage["cost"],
                "tool_calls": tool_calls,
                "blocked_tools": blocked_tools,
                "permission_decisions": int(permissions["total"] or 0),
                "permission_denied": int(permissions["denied"] or 0),
                "permission_asked": int(permissions["asked"] or 0),
            }
        finally:
            conn.close()

    def research_snapshot(self, research_run_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM workflow_snapshots WHERE research_run_id=?",
                (research_run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(research_run_id)
            return {
                "research_run_id": row["research_run_id"],
                "updated_at": row["updated_at"],
                "run": json.loads(row["run_json"]),
                "bindings": json.loads(row["bindings_json"]),
            }
        finally:
            conn.close()

    @staticmethod
    def _runtime_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "event_type": row["event_type"],
            "ts": row["ts"],
            "run_id": row["run_id"],
            "agent_id": row["agent_id"],
            "tool_call_id": row["tool_call_id"],
            "task_id": row["task_id"],
            "payload": json.loads(row["payload_json"]),
        }
