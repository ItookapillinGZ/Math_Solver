from __future__ import annotations

import builtins
import threading
from collections import deque
from collections.abc import Callable


class TerminalEventRenderer:
    """Serialize CLI output without letting background threads corrupt input.

    While interactive input is active, background-thread events are queued
    instead of printing immediately. The main thread flushes them at safe
    boundaries (before/after ``input`` and between turns). This intentionally
    trades truly live background logs for deterministic terminal rendering:
    no duplicate prompt redraws and no logs appended to an active prompt.
    """

    def __init__(
        self,
        prompt: str,
        *,
        input_fn: Callable[[str], str] | None = None,
        output_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.prompt = prompt
        self._input_fn = input_fn or builtins.input
        self._output_fn = output_fn or print
        self._interactive = False
        self._pending: deque[str] = deque()
        self._lock = threading.RLock()
        self._main_thread_id = threading.main_thread().ident

    @property
    def interactive(self) -> bool:
        with self._lock:
            return self._interactive

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def set_interactive(self, active: bool) -> None:
        with self._lock:
            self._interactive = bool(active)
        if not active:
            self.flush_pending()

    def emit(self, text: str = "") -> None:
        """Render a line or queue it when emitted by a background thread."""
        message = str(text)
        is_main_thread = threading.get_ident() == self._main_thread_id
        with self._lock:
            if self._interactive and not is_main_thread:
                self._pending.append(message)
                return
            self._output_fn(message)

    def flush_pending(self) -> list[str]:
        """Print queued background events in FIFO order from a safe boundary."""
        with self._lock:
            if not self._pending:
                return []
            pending = list(self._pending)
            self._pending.clear()
            for message in pending:
                self._output_fn(message)
            return pending

    def read_input(self, prompt: str | None = None) -> str:
        """Read one CLI line while keeping background output off the prompt."""
        self.flush_pending()
        query = self._input_fn(self.prompt if prompt is None else prompt)
        self.flush_pending()
        return query
