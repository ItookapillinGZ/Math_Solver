from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .models import MemoryKind
from .store import GLOBAL_SCOPE_ID, LEGACY_SCOPE_ID, MemoryStore


@dataclass(frozen=True)
class MemoryRuntimeConfig:
    workspace: Path
    legacy_memory_path: Path
    context_max_chars: int = 6000


@dataclass(frozen=True)
class MemoryRuntimeDependencies:
    store: MemoryStore
    resolve_path: Callable[[str], Path]
    terminal_print: Callable[[str], None] = print


class MemoryRuntime:
    """Application-facing memory operations over the durable MemoryStore.

    MemoryStore owns persistence and queries. MemoryRuntime owns the active
    problem scope, tool-facing CRUD semantics, workspace-safe artifact paths,
    legacy migration, and the context fragment consumed by the Lead agent.
    """

    def __init__(
        self,
        config: MemoryRuntimeConfig,
        dependencies: MemoryRuntimeDependencies,
    ) -> None:
        self.config = config
        self.deps = dependencies
        self.store = dependencies.store
        self._active_scope_id: str | None = None

    @property
    def active_scope_id(self) -> str:
        if self._active_scope_id is None:
            raise RuntimeError("MemoryRuntime session has not been initialized")
        return self._active_scope_id

    def initialize_session(
        self,
        *,
        scope_id: str | None = None,
        label: str | None = None,
    ) -> str:
        """Migrate legacy memory once and create the current runtime scope."""
        if self.store.import_legacy_memory_file(self.config.legacy_memory_path):
            self.deps.terminal_print(
                "  \033[34m[memory] migrated legacy .memory/MEMORY.md to SQLite\033[0m"
            )

        now = datetime.now()
        session_id = scope_id or (
            f"session_{now.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_"
            f"{random.randint(0, 999999):06d}"
        )
        session_label = label or f"Runtime session {now.isoformat(timespec='seconds')}"
        scope = self.store.create_scope(
            session_label,
            scope_id=session_id,
            metadata={"source": "runtime_session"},
        )
        self._active_scope_id = scope.id
        return scope.id

    @staticmethod
    def _record_json(record) -> str:
        return json.dumps(record.to_dict(), ensure_ascii=False, indent=2)

    def _scope_ids(self, scope: str, *, search_default_all: bool = False):
        normalized = (scope or "").strip().lower()
        if not normalized:
            normalized = "all" if search_default_all else "current"
        if normalized == "current":
            ids = [self.active_scope_id]
            if self.active_scope_id != GLOBAL_SCOPE_ID:
                ids.append(GLOBAL_SCOPE_ID)
            return ids
        if normalized == "global":
            return [GLOBAL_SCOPE_ID]
        if normalized == "all":
            return None
        raise ValueError("scope must be one of: current, global, all")

    def set_working(self, key: str, content: str, priority: int = 70) -> str:
        try:
            record = self.store.upsert_working(
                key,
                content,
                scope_id=self.active_scope_id,
                priority=priority,
                metadata={"source": "agent_tool"},
            )
            return self._record_json(record)
        except Exception as exc:
            return f"Error: {exc}"

    def add_episode(
        self,
        content: str,
        source: str = "agent",
        priority: int = 50,
    ) -> str:
        try:
            record = self.store.add_episode(
                content,
                scope_id=self.active_scope_id,
                source=source or "agent",
                priority=priority,
            )
            return self._record_json(record)
        except Exception as exc:
            return f"Error: {exc}"

    def add_artifact(
        self,
        path: str,
        description: str = "",
        artifact_type: str = "file",
        priority: int = 60,
    ) -> str:
        try:
            resolved = self.deps.resolve_path(path)
            relative = resolved.relative_to(self.config.workspace.resolve()).as_posix()
            record = self.store.upsert_artifact(
                relative,
                scope_id=self.active_scope_id,
                description=description,
                artifact_type=artifact_type,
                priority=priority,
                metadata={"source": "agent_tool"},
            )
            return self._record_json(record)
        except Exception as exc:
            return f"Error: {exc}"

    def list_memories(
        self,
        kind: str = "",
        scope: str = "current",
        limit: int = 20,
    ) -> str:
        try:
            records = self.store.list_memories(
                kind=MemoryKind(kind) if kind else None,
                scope_ids=self._scope_ids(scope),
                limit=limit,
            )
        except (ValueError, TypeError) as exc:
            return f"Error: {exc}"
        return json.dumps(
            [record.to_dict() for record in records],
            ensure_ascii=False,
            indent=2,
        )

    def search_memories(
        self,
        query: str,
        kind: str = "",
        scope: str = "all",
        limit: int = 20,
    ) -> str:
        try:
            kinds = [MemoryKind(kind)] if kind else None
            records = self.store.search_text(
                query,
                kinds=kinds,
                scope_ids=self._scope_ids(scope, search_default_all=True),
                limit=limit,
            )
        except (ValueError, TypeError) as exc:
            return f"Error: {exc}"
        return json.dumps(
            [record.to_dict() for record in records],
            ensure_ascii=False,
            indent=2,
        )

    def start_scope(self, label: str) -> str:
        try:
            previous = self.active_scope_id
            scope = self.store.create_scope(
                label,
                metadata={"source": "agent_tool", "previous_scope": previous},
            )
            if previous not in {GLOBAL_SCOPE_ID, LEGACY_SCOPE_ID} and previous != scope.id:
                try:
                    self.store.archive_scope(previous)
                except KeyError:
                    pass
            self._active_scope_id = scope.id
            return json.dumps(
                {"active_scope": scope.to_dict(), "previous_scope": previous},
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            return f"Error: {exc}"

    def switch_scope(self, scope_id: str) -> str:
        try:
            previous = self.active_scope_id
            scope = self.store.activate_scope(scope_id)
            if previous != scope.id and previous not in {GLOBAL_SCOPE_ID, LEGACY_SCOPE_ID}:
                try:
                    self.store.archive_scope(previous)
                except KeyError:
                    pass
            self._active_scope_id = scope.id
            return json.dumps(
                {"active_scope": scope.to_dict(), "previous_scope": previous},
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            return f"Error: {exc}"

    def list_scopes(self, include_archived: bool = True, limit: int = 100) -> str:
        try:
            scopes = self.store.list_scopes(
                include_archived=include_archived,
                limit=limit,
            )
            payload = []
            for scope in scopes:
                item = scope.to_dict()
                item["active"] = scope.id == self.active_scope_id
                payload.append(item)
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as exc:
            return f"Error: {exc}"

    def promote_global(self, memory_id: str) -> str:
        try:
            record = self.store.promote_to_global(memory_id)
            return self._record_json(record)
        except Exception as exc:
            return f"Error: {exc}"

    def build_context(self) -> str:
        return self.store.build_context(
            scope_id=self.active_scope_id,
            include_global=True,
            max_chars=self.config.context_max_chars,
        )
