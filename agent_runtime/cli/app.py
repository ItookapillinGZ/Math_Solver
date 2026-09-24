from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from .renderer import TerminalEventRenderer


@dataclass(frozen=True)
class AgentCLIApplicationDependencies:
    renderer: TerminalEventRenderer
    terminal_print: Callable[[str], None]
    trigger_hooks: Callable[..., Any]
    update_context: Callable[[dict, list], dict]
    agent_loop: Callable[[list, dict], Any]
    consume_lead_inbox: Callable[..., list[dict]]
    cron_scheduler: Any


class AgentCLIApplication:
    """Thin interactive shell around the already-assembled agent runtimes."""

    def __init__(self, dependencies: AgentCLIApplicationDependencies) -> None:
        self.deps = dependencies
        self.agent_lock = threading.Lock()

    def print_turn_assistants(self, messages: list, turn_start: int) -> None:
        for message in messages[turn_start:]:
            if message.get("role") != "assistant":
                continue
            content = message.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    self.deps.terminal_print(str(block.get("text", "")))
                elif getattr(block, "type", None) == "text":
                    self.deps.terminal_print(block.text)

    def cron_autorun_loop(self, history: list, context: dict) -> None:
        self.deps.cron_scheduler.autorun_loop(
            history,
            context,
            agent_lock=self.agent_lock,
            agent_loop=self.deps.agent_loop,
            update_context=self.deps.update_context,
            print_turn_assistants=self.print_turn_assistants,
        )

    @staticmethod
    def _inbox_label(message: dict) -> str:
        request_id = message.get("metadata", {}).get("request_id", "")
        suffix = f" req:{request_id}" if request_id else ""
        return f"{message.get('type', 'message')}{suffix}"

    def _append_inbox(self, history: list) -> None:
        inbox = self.deps.consume_lead_inbox(route_protocol=True)
        if not inbox:
            return
        inbox_text = "\n".join(
            f"From {message['from']} [{self._inbox_label(message)}]: "
            f"{message['content'][:200]}"
            for message in inbox
        )
        history.append({
            "role": "user",
            "content": f"[Inbox]\n{inbox_text}",
        })

    def run(self) -> None:
        renderer = self.deps.renderer
        renderer.set_interactive(True)
        self.deps.terminal_print("s20: comprehensive agent")
        self.deps.terminal_print(
            "Enter a question, press Enter to send. Type q to quit.\n"
        )

        history: list[dict] = []
        context = self.deps.update_context({}, [])
        cron_thread = threading.Thread(
            target=self.cron_autorun_loop,
            args=(history, context),
            daemon=True,
        )
        cron_thread.start()

        try:
            while True:
                try:
                    query = renderer.read_input()
                except (EOFError, KeyboardInterrupt):
                    break
                if query.strip().lower() in ("q", "exit", ""):
                    break

                self.deps.trigger_hooks("UserPromptSubmit", query)
                turn_start = len(history)
                history.append({"role": "user", "content": query})
                with self.agent_lock:
                    self.deps.agent_loop(history, context)
                    context = self.deps.update_context(context, history)
                    self.print_turn_assistants(history, turn_start)

                self._append_inbox(history)
                renderer.flush_pending()
                self.deps.terminal_print()
        finally:
            renderer.set_interactive(False)
