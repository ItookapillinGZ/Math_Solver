from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent_runtime.memory import MemoryStore

from .budget import ContextBudgetManager
from .checkpoint import (
    CompactionCheckpoint,
    build_checkpoint_prompt,
    parse_checkpoint_response,
)


@dataclass(frozen=True)
class ContextRuntimeConfig:
    transcript_dir: Path
    tool_results_dir: Path
    persist_threshold: int
    token_limit: int
    keep_recent_tool_results: int = 3
    soft_limit_ratio: float = 0.85
    tool_result_compact_threshold_tokens: int = 120
    tool_result_max_bytes: int = 200_000


@dataclass(frozen=True)
class ContextRuntimeDependencies:
    summarize_checkpoint: Callable[[str], str]
    memory_store: MemoryStore
    get_active_scope_id: Callable[[], str]
    terminal_print: Callable[[str], None] = print


class ContextRuntime:
    """Orchestrates context budgeting, transcripts, compaction and memory projection.

    Provider-specific model invocation stays outside this module. The runtime only
    asks for a checkpoint summary through ``summarize_checkpoint(prompt)`` and
    owns the deterministic parsing / fallback / persistence workflow around it.
    """

    def __init__(
        self,
        config: ContextRuntimeConfig,
        dependencies: ContextRuntimeDependencies,
    ) -> None:
        if config.persist_threshold < 0:
            raise ValueError("persist_threshold cannot be negative")
        if config.tool_result_max_bytes <= 0:
            raise ValueError("tool_result_max_bytes must be positive")

        self.config = config
        self.dependencies = dependencies
        self.budget = ContextBudgetManager(
            token_limit=config.token_limit,
            soft_limit_ratio=config.soft_limit_ratio,
            keep_recent_tool_results=config.keep_recent_tool_results,
            tool_result_compact_threshold_tokens=(
                config.tool_result_compact_threshold_tokens
            ),
        )

    def persist_large_output(self, tool_use_id: str, output: str) -> str:
        if len(output) <= self.config.persist_threshold:
            return output

        self.config.tool_results_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.tool_results_dir / f"{tool_use_id}.txt"
        # Tool output can contain mathematical Unicode even on GBK Windows hosts.
        # Rewrite an existing path as well: a previous interrupted write may be partial.
        path.write_text(output, encoding="utf-8")
        return (
            f"<persisted-output>\nFull output: {path}\n"
            f"Preview:\n{output[:2000]}\n</persisted-output>"
        )

    def tool_result_budget(
        self,
        messages: list,
        max_bytes: int | None = None,
    ) -> list:
        if not messages:
            return messages

        limit = self.config.tool_result_max_bytes if max_bytes is None else max_bytes
        if limit <= 0:
            raise ValueError("max_bytes must be positive")

        last = messages[-1]
        content = last.get("content")
        if last.get("role") != "user" or not isinstance(content, list):
            return messages

        blocks = [
            (index, block)
            for index, block in enumerate(content)
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        total = sum(len(str(block.get("content", ""))) for _, block in blocks)
        if total <= limit:
            return messages

        for _, block in sorted(
            blocks,
            key=lambda pair: len(str(pair[1].get("content", ""))),
            reverse=True,
        ):
            if total <= limit:
                break
            text = str(block.get("content", ""))
            block["content"] = self.persist_large_output(
                block.get("tool_use_id", "unknown"),
                text,
            )
            total = sum(
                len(str(candidate.get("content", "")))
                for _, candidate in blocks
            )
        return messages

    def write_transcript(self, messages: list) -> Path:
        import time

        self.config.transcript_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.transcript_dir / f"transcript_{int(time.time())}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for message in messages:
                handle.write(json.dumps(message, default=str) + "\n")
        return path

    def create_checkpoint(
        self,
        messages: list,
        transcript: Path,
    ) -> CompactionCheckpoint:
        prompt = build_checkpoint_prompt(messages)
        raw = self.dependencies.summarize_checkpoint(prompt) or ""
        return parse_checkpoint_response(
            raw,
            transcript_path=transcript,
        )

    def persist_checkpoint_memory(
        self,
        checkpoint: CompactionCheckpoint,
    ) -> None:
        scope_id = self.dependencies.get_active_scope_id()
        store = self.dependencies.memory_store

        working_items = {
            "current_goal": checkpoint.current_goal,
            "user_constraints": "\n".join(checkpoint.user_constraints),
            "active_tasks": "\n".join(checkpoint.active_tasks),
            "key_artifacts": "\n".join(checkpoint.key_artifacts),
            "important_decisions": "\n".join(checkpoint.important_decisions),
            "remaining_work": "\n".join(checkpoint.remaining_work),
        }
        for key, content in working_items.items():
            if content.strip():
                store.upsert_working(
                    key,
                    content,
                    scope_id=scope_id,
                    priority=(95 if key in {"user_constraints", "current_goal"} else 85),
                    metadata={"source": "compaction_checkpoint"},
                )

        if checkpoint.completed_work:
            store.add_episode(
                "Completed work from compaction checkpoint:\n- "
                + "\n- ".join(checkpoint.completed_work),
                scope_id=scope_id,
                source="compaction_checkpoint",
                priority=70,
                metadata={"transcript_path": checkpoint.transcript_path},
            )

        store.upsert_artifact(
            checkpoint.transcript_path,
            scope_id=scope_id,
            description="Full pre-compaction conversation transcript",
            artifact_type="transcript",
            priority=90,
            metadata={"source": "compaction_checkpoint"},
        )

    def _persist_checkpoint_memory_safely(
        self,
        checkpoint: CompactionCheckpoint,
    ) -> None:
        try:
            self.persist_checkpoint_memory(checkpoint)
        except Exception as exc:
            self.dependencies.terminal_print(
                f"  \033[33m[memory] checkpoint persistence warning: {exc}\033[0m"
            )

    def compact_history(self, messages: list) -> list:
        transcript = self.write_transcript(messages)
        self.dependencies.terminal_print(
            f"  \033[36m[compact] transcript saved: {transcript}\033[0m"
        )
        try:
            checkpoint = self.create_checkpoint(messages, transcript)
        except Exception as exc:
            self.dependencies.terminal_print(
                f"  \033[33m[compact] structured checkpoint fallback: {exc}\033[0m"
            )
            checkpoint = CompactionCheckpoint.fallback(
                transcript_path=transcript,
                note=(
                    "Structured checkpoint extraction failed. Continue from the saved "
                    "transcript and the most recent explicit user goal."
                ),
            )

        self._persist_checkpoint_memory_safely(checkpoint)
        return [{
            "role": "user",
            "content": checkpoint.to_context_text(),
        }]

    def reactive_compact(self, messages: list) -> list:
        transcript = self.write_transcript(messages)
        self.dependencies.terminal_print(
            f"  \033[31m[reactive compact] transcript saved: {transcript}\033[0m"
        )
        try:
            checkpoint = self.create_checkpoint(messages, transcript)
        except Exception as exc:
            checkpoint = CompactionCheckpoint.fallback(
                transcript_path=transcript,
                note=(
                    "Earlier conversation required reactive compaction after a "
                    f"prompt-too-long condition. Checkpoint extraction failed: {exc}"
                ),
            )

        self._persist_checkpoint_memory_safely(checkpoint)
        return [{
            "role": "user",
            "content": checkpoint.to_context_text(
                label="Reactive Structured Compaction Checkpoint"
            ),
        }, *messages[-5:]]

    def prepare_context(self, messages: list) -> list:
        messages[:] = self.tool_result_budget(messages)
        prepared, report = self.budget.fit(messages)
        messages[:] = prepared

        if report.compacted_tool_results:
            self.dependencies.terminal_print(
                f"  \033[36m[context] compacted {report.compacted_tool_results} "
                f"older tool result(s): {report.before_tokens} -> "
                f"{report.after_tokens} estimated tokens\033[0m"
            )

        if report.needs_summary:
            self.dependencies.terminal_print(
                f"  \033[36m[context] estimated {report.after_tokens} tokens exceeds "
                f"budget {report.token_limit}; creating semantic summary\033[0m"
            )
            messages[:] = self.compact_history(messages)
        return messages
