from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHECKPOINT_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _normalize_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        value = [value]

    items: list[str] = []
    for item in value:
        text = _normalize_text(item)
        if text and text not in items:
            items.append(text)
    return tuple(items)


@dataclass(frozen=True)
class CompactionCheckpoint:
    """Structured state retained when older conversation history is compacted."""

    transcript_path: str
    current_goal: str = ""
    user_constraints: tuple[str, ...] = field(default_factory=tuple)
    completed_work: tuple[str, ...] = field(default_factory=tuple)
    active_tasks: tuple[str, ...] = field(default_factory=tuple)
    key_artifacts: tuple[str, ...] = field(default_factory=tuple)
    important_decisions: tuple[str, ...] = field(default_factory=tuple)
    remaining_work: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = field(default_factory=_utc_now)
    version: int = CHECKPOINT_VERSION

    @classmethod
    def from_mapping(
        cls,
        data: dict[str, Any],
        *,
        transcript_path: str | Path,
    ) -> "CompactionCheckpoint":
        # The runtime owns the authoritative transcript path. Never trust a
        # model-provided path for an internal artifact reference.
        return cls(
            transcript_path=str(transcript_path),
            current_goal=_normalize_text(data.get("current_goal")),
            user_constraints=_normalize_list(data.get("user_constraints")),
            completed_work=_normalize_list(data.get("completed_work")),
            active_tasks=_normalize_list(data.get("active_tasks")),
            key_artifacts=_normalize_list(data.get("key_artifacts")),
            important_decisions=_normalize_list(data.get("important_decisions")),
            remaining_work=_normalize_list(data.get("remaining_work")),
        )

    @classmethod
    def fallback(
        cls,
        *,
        transcript_path: str | Path,
        note: str,
    ) -> "CompactionCheckpoint":
        return cls(
            transcript_path=str(transcript_path),
            current_goal=_normalize_text(note),
            remaining_work=(
                "Consult the saved transcript if details omitted by reactive compaction are needed.",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "transcript_path": self.transcript_path,
            "current_goal": self.current_goal,
            "user_constraints": list(self.user_constraints),
            "completed_work": list(self.completed_work),
            "active_tasks": list(self.active_tasks),
            "key_artifacts": list(self.key_artifacts),
            "important_decisions": list(self.important_decisions),
            "remaining_work": list(self.remaining_work),
        }

    def to_context_text(self, *, label: str = "Structured Compaction Checkpoint") -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        return (
            f"[{label}]\n"
            "This checkpoint represents earlier conversation state. Preserve its "
            "constraints and continue from its remaining work. The transcript path "
            "is an authoritative pointer to the full pre-compaction history.\n\n"
            f"{payload}"
        )


def build_checkpoint_prompt(messages: list[dict], *, max_chars: int = 80_000) -> str:
    """Build a provider-neutral extraction prompt for structured compaction."""
    conversation = json.dumps(
        messages,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )[:max_chars]

    return (
        "Extract the durable working state from this agent conversation. "
        "Return ONLY one valid JSON object, with no markdown fences and no prose. "
        "Do not invent facts. Preserve explicit user constraints, file names, task IDs, "
        "important decisions, and unfinished work. Keep entries concise but specific.\n\n"
        "Required schema:\n"
        "{\n"
        '  "current_goal": "string",\n'
        '  "user_constraints": ["string"],\n'
        '  "completed_work": ["string"],\n'
        '  "active_tasks": ["string"],\n'
        '  "key_artifacts": ["string"],\n'
        '  "important_decisions": ["string"],\n'
        '  "remaining_work": ["string"]\n'
        "}\n\n"
        "Conversation:\n"
        + conversation
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("checkpoint response did not contain a valid JSON object")


def parse_checkpoint_response(
    text: str,
    *,
    transcript_path: str | Path,
) -> CompactionCheckpoint:
    data = _extract_json_object(text)
    return CompactionCheckpoint.from_mapping(
        data,
        transcript_path=transcript_path,
    )
