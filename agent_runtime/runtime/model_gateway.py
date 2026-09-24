from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class RecoveryState:
    """Mutable request-recovery state shared across one lead-agent turn."""

    current_model: str
    has_escalated: bool = False
    recovery_count: int = 0
    consecutive_529: int = 0
    has_attempted_reactive_compact: bool = False
    retry_count: int = 0


@dataclass(frozen=True)
class AnthropicGatewayConfig:
    """Anthropic-specific retry and fallback settings."""

    primary_model: str
    fallback_model: str | None
    max_retries: int
    max_consecutive_529: int
    base_delay_ms: int


class AnthropicModelGateway:
    """Own Anthropic request construction and provider-level recovery.

    The orchestration runtime should not know how Anthropic is called, how
    provider errors are classified, or when a fallback model is selected.
    This gateway keeps that provider-specific behavior behind one boundary.
    """

    def __init__(
        self,
        *,
        client: Any,
        prompt_builder: Callable[[dict], Any],
        config: AnthropicGatewayConfig,
        output: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ):
        self.client = client
        self.prompt_builder = prompt_builder
        self.config = config
        self.output = output
        self.sleep = sleep
        self.jitter = jitter

    def new_recovery_state(self) -> RecoveryState:
        return RecoveryState(current_model=self.config.primary_model)

    @staticmethod
    def is_prompt_too_long_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            ("prompt" in msg and "long" in msg)
            or "context_length_exceeded" in msg
            or "max_context_window" in msg
        )

    def retry_delay(self, attempt: int) -> float:
        base = min(self.config.base_delay_ms * (2 ** attempt), 32000) / 1000
        return base + self.jitter(0, base * 0.25)

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        return "ratelimit" in name or "429" in msg

    @staticmethod
    def _is_overloaded_error(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        return "overloaded" in name or "529" in msg or "overloaded" in msg

    def with_retry(self, fn: Callable[[], Any], state: RecoveryState) -> Any:
        for attempt in range(self.config.max_retries):
            try:
                result = fn()
                state.consecutive_529 = 0
                return result
            except Exception as exc:
                if self._is_rate_limit_error(exc):
                    state.retry_count += 1
                    delay = self.retry_delay(attempt)
                    self.output(
                        f"  \033[33m[429] retry {attempt + 1}/"
                        f"{self.config.max_retries} after {delay:.1f}s\033[0m"
                    )
                    self.sleep(delay)
                    continue

                if self._is_overloaded_error(exc):
                    state.consecutive_529 += 1
                    if (
                        state.consecutive_529 >= self.config.max_consecutive_529
                        and self.config.fallback_model
                    ):
                        state.current_model = self.config.fallback_model
                        state.consecutive_529 = 0
                        self.output(
                            "  \033[31m[529] switching to "
                            f"{self.config.fallback_model}\033[0m"
                        )
                    state.retry_count += 1
                    delay = self.retry_delay(attempt)
                    self.output(
                        f"  \033[33m[529] retry {attempt + 1}/"
                        f"{self.config.max_retries} after {delay:.1f}s\033[0m"
                    )
                    self.sleep(delay)
                    continue

                raise

        raise RuntimeError(
            f"Max retries ({self.config.max_retries}) exceeded"
        )

    @staticmethod
    def sanitize_tool_inputs(messages: list) -> None:
        """Repair malformed historical tool_use inputs in-place.

        Earlier compaction or serialization steps can accidentally turn a
        tool input dict into a JSON string or an empty list. Anthropic expects
        tool_use.input to be an object, so normalize those known cases before
        each provider call.
        """

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if "input" not in block:
                    continue
                value = block["input"]
                if isinstance(value, str):
                    try:
                        parsed = json.loads(value)
                    except Exception:
                        continue
                    if isinstance(parsed, dict):
                        block["input"] = parsed
                elif value == []:
                    block["input"] = {}

    def call(
        self,
        messages: list,
        context: dict,
        tools: list,
        state: RecoveryState,
        max_tokens: int,
    ) -> Any:
        system = self.prompt_builder(context)
        self.sanitize_tool_inputs(messages)

        return self.with_retry(
            lambda: self.client.messages.create(
                model=state.current_model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
            ),
            state,
        )
