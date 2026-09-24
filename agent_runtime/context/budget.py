from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextBudgetReport:
    before_tokens: int
    after_tokens: int
    token_limit: int
    soft_limit: int
    compacted_tool_results: int
    needs_summary: bool


class HeuristicTokenEstimator:
    """Fast local token estimator with no provider/network dependency.

    This is deliberately an estimate rather than a tokenizer-specific exact
    count. ASCII text is approximated near four characters per token while
    non-ASCII text (including CJK) is weighted more heavily. Structured values
    are serialized before counting so tool inputs/results contribute to budget.
    """

    def estimate_text(self, text: str) -> int:
        if not text:
            return 0

        units = 0.0
        for char in text:
            codepoint = ord(char)
            if codepoint > 127:
                # CJK and other non-ASCII scripts commonly consume tokens more
                # densely than English prose.
                units += 0.8
            elif char.isspace():
                units += 0.05
            else:
                units += 0.25
        return max(1, math.ceil(units))

    def estimate_value(self, value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, str):
            return self.estimate_text(value)
        try:
            serialized = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            serialized = str(value)
        return self.estimate_text(serialized)

    def estimate_messages(self, messages: list[dict]) -> int:
        total = 0
        for message in messages:
            # Small role/message framing overhead.
            total += 4
            total += self.estimate_value(message.get("role", ""))
            total += self.estimate_value(message.get("content", ""))
        return total


class ContextBudgetManager:
    """Token-aware, priority-aware local context budget manager.

    Priority policy in this first version:
      * recent tool results are protected;
      * older tool results are the first content compacted;
      * ordinary user/assistant prose is never silently dropped locally;
      * if low-priority compaction cannot fit the hard token limit, the caller
        is told to use its semantic/LLM summarization path.

    This keeps user constraints and recent conversation intact while removing
    the least valuable historical bulk first.
    """

    COMPACTED_TOOL_RESULT = (
        "[Earlier tool result compacted by context budget manager. "
        "Re-run the tool if the full output is needed.]"
    )

    def __init__(
        self,
        token_limit: int,
        *,
        soft_limit_ratio: float = 0.85,
        keep_recent_tool_results: int = 3,
        tool_result_compact_threshold_tokens: int = 120,
        estimator: HeuristicTokenEstimator | None = None,
    ):
        if token_limit <= 0:
            raise ValueError("token_limit must be positive")
        if not 0 < soft_limit_ratio <= 1:
            raise ValueError("soft_limit_ratio must be in (0, 1]")
        if keep_recent_tool_results < 0:
            raise ValueError("keep_recent_tool_results cannot be negative")
        if tool_result_compact_threshold_tokens < 0:
            raise ValueError("tool_result_compact_threshold_tokens cannot be negative")

        self.token_limit = token_limit
        self.soft_limit = max(1, int(token_limit * soft_limit_ratio))
        self.keep_recent_tool_results = keep_recent_tool_results
        self.tool_result_compact_threshold_tokens = (
            tool_result_compact_threshold_tokens
        )
        self.estimator = estimator or HeuristicTokenEstimator()

    def estimate(self, messages: list[dict]) -> int:
        return self.estimator.estimate_messages(messages)

    @staticmethod
    def _tool_results(messages: list[dict]) -> list[tuple[int, int, dict]]:
        results: list[tuple[int, int, dict]] = []
        for message_index, message in enumerate(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block_index, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    results.append((message_index, block_index, block))
        return results

    def fit(self, messages: list[dict]) -> tuple[list[dict], ContextBudgetReport]:
        prepared = copy.deepcopy(messages)
        before_tokens = self.estimate(prepared)
        compacted = 0

        # Do nothing while comfortably under budget. This avoids unnecessary
        # history degradation on short conversations.
        if before_tokens > self.soft_limit:
            tool_results = self._tool_results(prepared)
            protected_start = max(
                0,
                len(tool_results) - self.keep_recent_tool_results,
            )
            candidates = tool_results[:protected_start]

            # Within the low-priority historical bucket, compact the largest
            # outputs first to gain the most budget with the fewest mutations.
            candidates = sorted(
                candidates,
                key=lambda item: self.estimator.estimate_value(
                    item[2].get("content", "")
                ),
                reverse=True,
            )

            current_tokens = before_tokens
            for _, _, block in candidates:
                if current_tokens <= self.soft_limit:
                    break
                content = block.get("content", "")
                content_tokens = self.estimator.estimate_value(content)
                if content_tokens < self.tool_result_compact_threshold_tokens:
                    continue
                block["content"] = self.COMPACTED_TOOL_RESULT
                compacted += 1
                current_tokens = self.estimate(prepared)

        after_tokens = self.estimate(prepared)
        report = ContextBudgetReport(
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            token_limit=self.token_limit,
            soft_limit=self.soft_limit,
            compacted_tool_results=compacted,
            needs_summary=after_tokens > self.token_limit,
        )
        return prepared, report
