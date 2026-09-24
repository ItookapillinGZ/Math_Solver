import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import PermissionDecision


@dataclass(frozen=True)
class PolicyAuditEvent:
    """One durable record of a permission decision.

    Raw tool inputs are intentionally not stored here. Commands and file
    contents may contain secrets, so the audit log keeps only decision
    metadata needed for debugging and review.
    """

    timestamp: str
    tool_name: str
    rule_id: str
    action: str
    reason: str
    user_approved: bool | None
    outcome: str


class PolicyAuditLogger:
    """Append policy decisions to a thread-safe JSONL audit log."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(
        self,
        tool_name: str,
        decision: PermissionDecision,
        *,
        user_approved: bool | None = None,
    ) -> PolicyAuditEvent:
        event = PolicyAuditEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            tool_name=tool_name,
            rule_id=decision.rule_id,
            action=decision.action.value,
            reason=decision.reason,
            user_approved=user_approved,
            outcome=self._outcome(decision, user_approved),
        )
        payload = json.dumps(asdict(event), ensure_ascii=False)

        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(payload + "\n")

        return event

    def read_recent(self, limit: int = 20) -> list[dict]:
        """Return up to ``limit`` most recent audit records."""
        if limit <= 0 or not self.path.exists():
            return []

        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()

        records = []
        for line in lines[-limit:]:
            if not line.strip():
                continue
            records.append(json.loads(line))
        return records

    @staticmethod
    def _outcome(
        decision: PermissionDecision,
        user_approved: bool | None,
    ) -> str:
        if decision.action.value == "allow":
            return "allowed"
        if decision.action.value == "deny":
            return "denied_by_policy"
        if user_approved is True:
            return "approved_by_user"
        if user_approved is False:
            return "denied_by_user"
        return "approval_required"
