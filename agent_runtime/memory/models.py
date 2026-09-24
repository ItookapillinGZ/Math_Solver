from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryKind(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    ARTIFACT = "artifact"


@dataclass(frozen=True)
class MemoryScope:
    id: str
    label: str
    archived_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "archived": self.archived,
            "archived_at": self.archived_at,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    kind: MemoryKind
    content: str
    key: str | None = None
    priority: int = 50
    scope_id: str = "global"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "scope_id": self.scope_id,
            "key": self.key,
            "content": self.content,
            "priority": self.priority,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
