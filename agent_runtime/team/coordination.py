from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .store import MessageStore


@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    created_at: float = field(default_factory=time.time)


class MessageBus:
    """Durable SQLite-backed team mailbox bus.

    The public API intentionally preserves the original ``send`` / ``read_inbox``
    contract. ``read_inbox`` remains read-and-consume, while MessageStore makes
    that transition atomic across threads and processes.
    """

    def __init__(
        self,
        store: MessageStore,
        terminal_print: Callable[[str], None] | None = None,
        *,
        legacy_mailbox_dir: Path | None = None,
    ) -> None:
        self.store = store
        self._terminal_print = terminal_print or print
        if legacy_mailbox_dir is not None:
            result = self.store.migrate_legacy_mailboxes(legacy_mailbox_dir)
            if result["imported"] or result["errors"]:
                self._terminal_print(
                    "  \033[33m[bus migration] "
                    f"imported={result['imported']} "
                    f"duplicates={result['duplicates']} "
                    f"errors={result['errors']}\033[0m"
                )

    def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        msg_type: str = "message",
        metadata: dict | None = None,
    ) -> dict:
        record = self.store.send(
            from_agent,
            to_agent,
            content,
            msg_type,
            metadata,
        )
        msg = record.to_message()
        self._terminal_print(
            f"  \033[33m[bus] {from_agent} → {to_agent}: "
            f"({msg_type}) {content[:50]}\033[0m"
        )
        return msg

    def read_inbox(self, agent: str) -> list[dict]:
        return [record.to_message() for record in self.store.consume_inbox(agent)]


class TeamCoordinator:
    """Own team messaging plus request/response protocol state.

    Step 10A moves team messages to durable SQLite storage while intentionally
    leaving ProtocolState process-local. This keeps the protocol API stable and
    isolates the persistence change to MessageBus.
    """

    def __init__(
        self,
        mailbox_dir: Path | None = None,
        terminal_print: Callable[[str], None] | None = None,
        lead_name: str = "lead",
        *,
        message_db_path: Path | None = None,
        message_store: MessageStore | None = None,
    ) -> None:
        self.lead_name = lead_name
        if message_store is None:
            if message_db_path is None:
                if mailbox_dir is None:
                    raise ValueError(
                        "mailbox_dir, message_db_path, or message_store is required"
                    )
                # Compatibility default for older callers/tests. Production
                # wiring passes .state/team.db explicitly.
                message_db_path = Path(mailbox_dir) / "messages.db"
            message_store = MessageStore(message_db_path)
        self.bus = MessageBus(
            message_store,
            terminal_print=terminal_print,
            legacy_mailbox_dir=mailbox_dir,
        )
        self.pending_requests: dict[str, ProtocolState] = {}
        self._lock = threading.RLock()

    def new_request_id(self) -> str:
        with self._lock:
            while True:
                request_id = f"req_{random.randint(0, 999999):06d}"
                if request_id not in self.pending_requests:
                    return request_id

    def get_request(self, request_id: str) -> ProtocolState | None:
        with self._lock:
            return self.pending_requests.get(request_id)

    def match_response(
        self,
        response_type: str,
        request_id: str,
        approve: bool,
    ) -> ProtocolState | None:
        with self._lock:
            state = self.pending_requests.get(request_id)
            if not state:
                return None
            if state.type == "shutdown" and response_type != "shutdown_response":
                return state
            if (
                state.type == "plan_approval"
                and response_type != "plan_approval_response"
            ):
                return state
            state.status = "approved" if approve else "rejected"
            return state

    def consume_lead_inbox(self, route_protocol: bool = True) -> list[dict]:
        msgs = self.bus.read_inbox(self.lead_name)
        if route_protocol:
            for msg in msgs:
                meta = msg.get("metadata", {})
                request_id = meta.get("request_id", "")
                msg_type = msg.get("type", "")
                if request_id and msg_type.endswith("_response"):
                    self.match_response(
                        msg_type,
                        request_id,
                        bool(meta.get("approve", False)),
                    )
        return msgs

    def submit_plan(self, from_name: str, plan: str) -> str:
        request_id = self.new_request_id()
        with self._lock:
            self.pending_requests[request_id] = ProtocolState(
                request_id=request_id,
                type="plan_approval",
                sender=from_name,
                target=self.lead_name,
                status="pending",
                payload=plan,
            )
        self.bus.send(
            from_name,
            self.lead_name,
            plan,
            "plan_approval_request",
            {"request_id": request_id},
        )
        return f"Plan submitted ({request_id})"

    def request_shutdown(self, teammate: str) -> str:
        request_id = self.new_request_id()
        with self._lock:
            self.pending_requests[request_id] = ProtocolState(
                request_id=request_id,
                type="shutdown",
                sender=self.lead_name,
                target=teammate,
                status="pending",
                payload="",
            )
        self.bus.send(
            self.lead_name,
            teammate,
            "Shut down.",
            "shutdown_request",
            {"request_id": request_id},
        )
        return f"Shutdown request sent to {teammate}"

    def request_plan(self, teammate: str, task: str) -> str:
        self.bus.send(
            self.lead_name,
            teammate,
            f"Submit plan for: {task}",
            "message",
        )
        return f"Asked {teammate} to submit a plan"

    def review_plan(
        self,
        request_id: str,
        approve: bool,
        feedback: str = "",
    ) -> str:
        with self._lock:
            state = self.pending_requests.get(request_id)
            if not state:
                return f"Request {request_id} not found"
            if state.type != "plan_approval":
                return f"Request {request_id} is not a plan approval request"
            state.status = "approved" if approve else "rejected"
            sender = state.sender

        self.bus.send(
            self.lead_name,
            sender,
            feedback or ("Approved" if approve else "Rejected"),
            "plan_approval_response",
            {"request_id": request_id, "approve": approve},
        )
        return f"Plan {'approved' if approve else 'rejected'}"

    def send_from_lead(self, to: str, content: str) -> str:
        self.bus.send(self.lead_name, to, content)
        return f"Sent to {to}"

    def format_lead_inbox(self) -> str:
        msgs = self.consume_lead_inbox(route_protocol=True)
        if not msgs:
            return "(inbox empty)"
        lines = []
        for msg in msgs:
            meta = msg.get("metadata", {})
            request_id = meta.get("request_id", "")
            tag = (
                f" [{msg['type']} req:{request_id}]"
                if request_id
                else f" [{msg['type']}]"
            )
            lines.append(f"  [{msg['from']}]{tag} {msg['content'][:200]}")
        return "\n".join(lines)
