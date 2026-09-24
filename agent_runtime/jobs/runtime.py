from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .cron import (
    claim_scheduled_prompts,
    enqueue_scheduled_prompt,
    migrate_legacy_schedule_items,
    reconcile_one_shot_schedules,
)
from .models import JobRecord, JobStatus
from .runner import ThreadJobRunner
from .store import JobStore


TerminalPrinter = Callable[[str], None]
ToolCaller = Callable[[Callable[..., Any] | None, dict[str, Any], str], str]
HookTrigger = Callable[..., Any]


@dataclass(frozen=True)
class BackgroundJobRuntimeDependencies:
    call_tool_handler: ToolCaller
    trigger_hooks: HookTrigger
    terminal_print: TerminalPrinter


class BackgroundJobRuntime:
    """Application-facing orchestration for durable background tool jobs.

    JobStore owns durability and ThreadJobRunner owns execution threads. This
    class only translates agent tool calls into durable jobs, executes the
    registered handler, and converts terminal job states back into model-facing
    notifications.
    """

    SLOW_BASH_KEYWORDS = (
        "install",
        "build",
        "test",
        "deploy",
        "compile",
        "docker build",
        "pip install",
        "npm install",
        "cargo build",
        "pytest",
        "make",
    )

    def __init__(
        self,
        store: JobStore,
        dependencies: BackgroundJobRuntimeDependencies,
        *,
        runner_prefix: str = "background",
    ) -> None:
        self.store = store
        self.dependencies = dependencies
        self.runner = ThreadJobRunner(store, runner_prefix=runner_prefix)

    def recover_interrupted(self) -> list[JobRecord]:
        # Background tool calls may have side effects, so interrupted RUNNING
        # jobs are not replayed automatically when max_attempts=1.
        return self.store.recover_interrupted_jobs(source="background")

    @classmethod
    def is_slow_operation(cls, tool_name: str, tool_input: dict[str, Any]) -> bool:
        if tool_name != "bash":
            return False
        command = str(tool_input.get("command", "")).lower()
        return any(keyword in command for keyword in cls.SLOW_BASH_KEYWORDS)

    @classmethod
    def should_run(cls, tool_name: str, tool_input: dict[str, Any]) -> bool:
        if tool_name != "bash":
            return False
        return bool(tool_input.get("run_in_background")) or cls.is_slow_operation(
            tool_name, tool_input
        )

    def _execute_job(self, job: JobRecord, handlers: dict[str, Callable[..., Any]]) -> dict[str, str]:
        payload = job.payload
        tool_name = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input") or {}
        handler = handlers.get(tool_name)
        result = self.dependencies.call_tool_handler(handler, tool_input, tool_name)

        # Reconstruct only the attributes PostToolUse hooks rely on. Keeping
        # this tiny adapter avoids coupling the job layer to Anthropic classes.
        class _BackgroundBlock:
            pass

        block = _BackgroundBlock()
        block.id = payload.get("tool_use_id", job.id)
        block.name = tool_name
        block.input = tool_input
        self.dependencies.trigger_hooks("PostToolUse", block, result)
        return {"output": str(result)}

    def start(self, block: Any, handlers: dict[str, Callable[..., Any]]) -> str:
        command = block.input.get("command", block.name)
        job = self.store.create_job(
            "tool_call",
            {
                "tool_use_id": block.id,
                "tool_name": block.name,
                "tool_input": dict(block.input),
                "command": command,
            },
            source="background",
            timeout_seconds=120 if block.name == "bash" else None,
            # Do not automatically replay potentially side-effecting tool calls.
            max_attempts=1,
        )
        self.runner.start(
            job.id,
            lambda claimed: self._execute_job(claimed, handlers),
        )
        self.dependencies.terminal_print(
            f"  \033[33m[background] {job.id}: {str(command)[:60]}\033[0m"
        )
        return job.id

    def resume_queued(self, handlers: dict[str, Callable[..., Any]]) -> list[str]:
        return self.runner.resume_queued(
            lambda claimed: self._execute_job(claimed, handlers),
            source="background",
            kind="tool_call",
        )

    def collect_notifications(self) -> list[str]:
        notifications: list[str] = []
        jobs = self.store.list_unnotified_terminal(
            source="background",
            kind="tool_call",
        )
        for job in jobs:
            payload = job.payload
            command = payload.get(
                "command",
                payload.get("tool_name", "background job"),
            )
            if job.status == JobStatus.SUCCEEDED:
                result = job.result or {}
                output = (
                    result.get("output", "")
                    if isinstance(result, dict)
                    else str(result)
                )
                summary = output[:200] if len(output) > 200 else output
            else:
                summary = (job.error or job.status.value)[:200]
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{job.id}</task_id>\n"
                f"  <status>{job.status.value}</status>\n"
                f"  <command>{command}</command>\n"
                f"  <summary>{summary}</summary>\n"
                f"</task_notification>"
            )
            self.store.mark_notified(job.id)
        return notifications

    def build_user_content(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        content = list(results)
        for note in self.collect_notifications():
            content.append({"type": "text", "text": note})
        return content

    def inject_notifications(self, messages: list[dict[str, Any]]) -> None:
        notes = self.collect_notifications()
        if notes:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": note}
                        for note in notes
                    ],
                }
            )

    def list_jobs_text(self, status: str = "") -> str:
        try:
            jobs = self.store.list_jobs(
                status=JobStatus(status) if status else None,
                source="background",
                kind="tool_call",
            )
        except ValueError:
            return (
                "Error: status must be queued, running, succeeded, failed, "
                "cancelled, or timed_out"
            )
        if not jobs:
            return "No background jobs."
        return "\n".join(
            f"  {job.id}: "
            f"{job.payload.get('command', job.payload.get('tool_name', ''))} "
            f"[{job.status.value}] attempt={job.attempt_count}/{job.max_attempts}"
            for job in jobs
        )

    def get_job_text(self, job_id: str) -> str:
        try:
            job = self.store.get_job(job_id)
        except KeyError:
            return f"Error: background job {job_id} not found"
        if job.source != "background":
            return f"Error: job {job_id} is not a background tool job"
        return json.dumps(
            {
                "id": job.id,
                "status": job.status.value,
                "command": job.payload.get("command", ""),
                "tool_name": job.payload.get("tool_name", ""),
                "runner_id": job.runner_id,
                "attempt_count": job.attempt_count,
                "max_attempts": job.max_attempts,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "error": job.error,
                "result": job.result,
            },
            ensure_ascii=False,
            indent=2,
        )


@dataclass(frozen=True)
class CronSchedulerConfig:
    legacy_path: Path
    runner_id: str
    session_id: str
    poll_interval_seconds: float = 1.0


@dataclass(frozen=True)
class CronSchedulerDependencies:
    terminal_print: TerminalPrinter
    now: Callable[[], datetime] = datetime.now
    sleep: Callable[[float], None] = time.sleep


class CronSchedulerRuntime:
    """Durable cron schedule orchestration over JobStore.

    Schedule definitions and firing occurrences are persisted by JobStore. This
    class owns cron validation/matching, legacy migration, polling, delivery to
    the lead agent, and session visibility rules.
    """

    def __init__(
        self,
        store: JobStore,
        config: CronSchedulerConfig,
        dependencies: CronSchedulerDependencies,
    ) -> None:
        self.store = store
        self.config = config
        self.dependencies = dependencies
        self._scheduler_thread: threading.Thread | None = None
        self._scheduler_lock = threading.Lock()

    def recover_interrupted(self) -> list[JobRecord]:
        # Scheduled prompts are intentionally at-least-once. Interrupted
        # delivery jobs are re-queued while retry budget remains.
        return self.store.recover_interrupted_jobs(
            source="cron",
            kind="scheduled_prompt",
        )

    @staticmethod
    def _field_matches(field: str, value: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            step = int(field[2:])
            return step > 0 and value % step == 0
        if "," in field:
            return any(
                CronSchedulerRuntime._field_matches(part.strip(), value)
                for part in field.split(",")
            )
        if "-" in field:
            lo, hi = field.split("-", 1)
            return int(lo) <= value <= int(hi)
        return value == int(field)

    @classmethod
    def matches(cls, cron_expr: str, dt: datetime) -> bool:
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return False
        minute, hour, dom, month, dow = fields
        dow_val = (dt.weekday() + 1) % 7
        minute_ok = cls._field_matches(minute, dt.minute)
        hour_ok = cls._field_matches(hour, dt.hour)
        dom_ok = cls._field_matches(dom, dt.day)
        month_ok = cls._field_matches(month, dt.month)
        dow_ok = cls._field_matches(dow, dow_val)
        if not (minute_ok and hour_ok and month_ok):
            return False
        if dom == "*" and dow == "*":
            return True
        if dom == "*":
            return dow_ok
        if dow == "*":
            return dom_ok
        # Standard cron semantics: when both DOM and DOW are restricted,
        # either field matching is enough.
        return dom_ok or dow_ok

    @classmethod
    def _validate_field(cls, field: str, lo: int, hi: int) -> str | None:
        if field == "*":
            return None
        if field.startswith("*/"):
            step = field[2:]
            if not step.isdigit() or int(step) <= 0:
                return f"Invalid step: {field}"
            return None
        if "," in field:
            for part in field.split(","):
                err = cls._validate_field(part.strip(), lo, hi)
                if err:
                    return err
            return None
        if "-" in field:
            left, right = field.split("-", 1)
            if not left.isdigit() or not right.isdigit():
                return f"Invalid range: {field}"
            a, b = int(left), int(right)
            if a < lo or a > hi or b < lo or b > hi:
                return f"Range {field} out of bounds [{lo}-{hi}]"
            if a > b:
                return f"Range start > end: {field}"
            return None
        if not field.isdigit():
            return f"Invalid field: {field}"
        value = int(field)
        if value < lo or value > hi:
            return f"Value {value} out of bounds [{lo}-{hi}]"
        return None

    @classmethod
    def validate(cls, cron_expr: str) -> str | None:
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return f"Expected 5 fields, got {len(fields)}"
        bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
        names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
        for field, (lo, hi), name in zip(fields, bounds, names):
            err = cls._validate_field(field, lo, hi)
            if err:
                return f"{name}: {err}"
        return None

    def migrate_legacy_file(self) -> tuple[int, int]:
        path = self.config.legacy_path
        if not path.exists():
            return (0, 0)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("legacy cron file must contain a JSON list")
            imported, skipped = migrate_legacy_schedule_items(
                self.store,
                raw,
                validate_cron=self.validate,
            )
            # Only remove the legacy file once the entire migration succeeds.
            path.unlink()
            if imported or skipped:
                self.dependencies.terminal_print(
                    "\033[90m[cron migration] "
                    f"imported={imported} skipped={skipped}\033[0m"
                )
            return imported, skipped
        except Exception as exc:
            self.dependencies.terminal_print(
                "\033[31m[cron migration error] keeping legacy file: "
                f"{exc}\033[0m"
            )
            return (0, 0)

    def visible_schedules(self):
        return self.store.list_schedules(
            active_only=True,
            session_id=self.config.session_id,
        )

    def reconcile_one_shots(self) -> int:
        recovered = reconcile_one_shot_schedules(
            self.store,
            self.visible_schedules(),
        )
        if recovered:
            self.dependencies.terminal_print(
                "\033[90m[cron recovery] deactivated "
                f"{recovered} fired one-shot schedule(s)\033[0m"
            )
        return recovered

    def schedule(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = True,
    ):
        err = self.validate(cron)
        if err:
            return err
        return self.store.create_schedule(
            cron,
            prompt,
            recurring=recurring,
            durable=durable,
            session_id=None if durable else self.config.session_id,
        )

    def cancel(self, schedule_id: str) -> str:
        try:
            schedule = self.store.get_schedule(schedule_id)
        except KeyError:
            return f"Job {schedule_id} not found"
        if not schedule.active:
            return f"Job {schedule_id} not found"
        if (
            not schedule.durable
            and schedule.session_id != self.config.session_id
        ):
            return f"Job {schedule_id} not found"
        self.store.cancel_schedule(schedule_id)
        return f"Cancelled {schedule_id}"

    def tick(self, now: datetime | None = None) -> list[str]:
        now = now or self.dependencies.now()
        marker = now.strftime("%Y-%m-%d %H:%M")
        created_ids: list[str] = []
        for schedule in self.visible_schedules():
            try:
                if not self.matches(schedule.cron, now):
                    continue
                if schedule.last_fired_marker == marker:
                    continue

                occurrence, created = enqueue_scheduled_prompt(
                    self.store,
                    schedule_id=schedule.id,
                    cron=schedule.cron,
                    prompt=schedule.prompt,
                    marker=marker,
                    fired_at=now,
                )
                # The occurrence is durable before advancing the firing marker.
                # For one-shots this also atomically deactivates the schedule.
                self.store.mark_schedule_fired(schedule.id, marker)
                if created:
                    created_ids.append(occurrence.id)
                    self.dependencies.terminal_print(
                        f"  \033[35m[cron queued] {schedule.id} -> "
                        f"{occurrence.id}\033[0m"
                    )
            except Exception as exc:
                self.dependencies.terminal_print(
                    f"  \033[31m[cron error] {schedule.id}: {exc}\033[0m"
                )
        return created_ids

    def scheduler_loop(self, stop_event: threading.Event | None = None) -> None:
        while stop_event is None or not stop_event.is_set():
            self.dependencies.sleep(self.config.poll_interval_seconds)
            if stop_event is not None and stop_event.is_set():
                break
            self.tick()

    def start_scheduler_thread(self) -> bool:
        with self._scheduler_lock:
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                return False
            thread = threading.Thread(
                target=self.scheduler_loop,
                daemon=True,
                name="cron-scheduler",
            )
            self._scheduler_thread = thread
            thread.start()
            return True

    def claim_prompt_jobs(self) -> list[JobRecord]:
        return claim_scheduled_prompts(self.store, self.config.runner_id)

    def mark_prompt_delivered(self, job: JobRecord) -> JobRecord:
        return self.store.mark_succeeded(
            job.id,
            self.config.runner_id,
            {"delivered": True},
        )

    def schedule_text(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = True,
    ) -> str:
        result = self.schedule(cron, prompt, recurring, durable)
        if isinstance(result, str):
            return f"Error: {result}"
        return f"Scheduled {result.id}: '{cron}' -> {prompt}"

    def list_text(self) -> str:
        schedules = self.visible_schedules()
        if not schedules:
            return "No cron jobs."
        return "\n".join(
            f"  {schedule.id}: '{schedule.cron}' -> {schedule.prompt[:40]} "
            f"[{'recurring' if schedule.recurring else 'one-shot'}, "
            f"{'durable' if schedule.durable else 'session'}]"
            for schedule in schedules
        )

    def deliver_claimed_once(
        self,
        history: list[dict[str, Any]],
        context: dict[str, Any],
        *,
        agent_lock: Any,
        agent_loop: Callable[[list[dict[str, Any]], dict[str, Any]], Any],
        update_context: Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]],
        print_turn_assistants: Callable[[list[dict[str, Any]], int], None],
    ) -> int:
        fired = self.claim_prompt_jobs()
        if not fired:
            return 0
        with agent_lock:
            turn_start = len(history)
            for job in fired:
                prompt = str(job.payload.get("prompt", ""))
                history.append(
                    {
                        "role": "user",
                        "content": f"[Scheduled] {prompt}",
                    }
                )
                self.dependencies.terminal_print(
                    f"  \033[35m[cron auto] {prompt[:60]}\033[0m"
                )
                try:
                    self.mark_prompt_delivered(job)
                except Exception as exc:
                    self.dependencies.terminal_print(
                        f"  \033[31m[cron delivery error] {job.id}: {exc}\033[0m"
                    )
            agent_loop(history, context)
            context.update(update_context(context, history))
            print_turn_assistants(history, turn_start)
        return len(fired)

    def autorun_loop(
        self,
        history: list[dict[str, Any]],
        context: dict[str, Any],
        *,
        agent_lock: Any,
        agent_loop: Callable[[list[dict[str, Any]], dict[str, Any]], Any],
        update_context: Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]],
        print_turn_assistants: Callable[[list[dict[str, Any]], int], None],
        stop_event: threading.Event | None = None,
    ) -> None:
        while stop_event is None or not stop_event.is_set():
            self.dependencies.sleep(self.config.poll_interval_seconds)
            if stop_event is not None and stop_event.is_set():
                break
            self.deliver_claimed_once(
                history,
                context,
                agent_lock=agent_lock,
                agent_loop=agent_loop,
                update_context=update_context,
                print_turn_assistants=print_turn_assistants,
            )

    def start_autorun_thread(
        self,
        history: list[dict[str, Any]],
        context: dict[str, Any],
        *,
        agent_lock: Any,
        agent_loop: Callable[[list[dict[str, Any]], dict[str, Any]], Any],
        update_context: Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]],
        print_turn_assistants: Callable[[list[dict[str, Any]], int], None],
    ) -> threading.Thread:
        thread = threading.Thread(
            target=self.autorun_loop,
            kwargs={
                "history": history,
                "context": context,
                "agent_lock": agent_lock,
                "agent_loop": agent_loop,
                "update_context": update_context,
                "print_turn_assistants": print_turn_assistants,
            },
            daemon=True,
            name="cron-autorun",
        )
        thread.start()
        return thread
