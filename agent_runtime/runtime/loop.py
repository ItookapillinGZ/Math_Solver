from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Any, Callable

from .execution import (
    AgentKind,
    ExecutionStatus,
    ExecutionTracker,
    RuntimeEventType,
    ToolCallStatus,
    response_usage_payload,
)


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Stable loop-level settings that do not depend on a model provider."""

    default_max_tokens: int
    escalated_max_tokens: int
    max_recovery_retries: int
    continuation_prompt: str
    todo_reminder_interval: int = 3


@dataclass(frozen=True)
class AgentRuntimeDependencies:
    """Callbacks supplied by the application layer.

    The runtime owns orchestration. Domain-specific behavior (tools, policy,
    jobs, prompt assembly, compaction, and model calls) remains injectable so
    this module does not import the monolithic application entrypoint.
    """

    assemble_tool_pool: Callable[[], tuple[list[dict], dict]]
    resume_queued_background_jobs: Callable[[dict], list[str]]
    claim_scheduled_prompts: Callable[[], list[Any]]
    mark_scheduled_prompt_delivered: Callable[[Any], None]
    inject_background_notifications: Callable[[list], None]
    prepare_context: Callable[[list], list]
    update_context: Callable[[dict, list], dict]
    call_llm: Callable[[list, dict, list, Any, int], Any]
    make_recovery_state: Callable[[], Any]
    is_prompt_too_long_error: Callable[[Exception], bool]
    reactive_compact: Callable[[list], list]
    has_tool_use: Callable[[Any], bool]
    trigger_hooks: Callable[..., Any]
    compact_history: Callable[[list], list]
    should_run_background: Callable[[str, dict], bool]
    start_background_task: Callable[[Any, dict], str]
    call_tool_handler: Callable[[Any, dict, str], str]
    build_user_content: Callable[[list[dict]], list[dict]]
    research_run_stage: Callable[[str], str | None] | None = None
    record_literature_tool_result: Callable[[str, str, dict, str], Any] | None = None
    record_literature_query_started: Callable[[str, str, dict], Any] | None = None


class AgentRuntime:
    """Provider-agnostic orchestration loop for the lead agent.

    This class deliberately owns only control flow. It does not know how tools
    are implemented, how permissions are decided, how jobs are stored, or how
    Anthropic is called. Those concerns are injected through dependencies.
    """

    def __init__(
        self,
        config: AgentRuntimeConfig,
        dependencies: AgentRuntimeDependencies,
        *,
        output: Callable[[str], None] = print,
        execution_tracker: ExecutionTracker | None = None,
    ):
        self.config = config
        self.deps = dependencies
        self.output = output
        self.execution_tracker = execution_tracker
        self.rounds_since_todo = 0
        self._research_runs_by_scope: dict[str, set[str]] = {}

    @staticmethod
    def _block_type(block: Any) -> str | None:
        if isinstance(block, dict):
            return block.get("type")
        return getattr(block, "type", None)

    @staticmethod
    def _block_name(block: Any) -> str:
        if isinstance(block, dict):
            return str(block.get("name", ""))
        return str(getattr(block, "name", ""))

    @staticmethod
    def _block_id(block: Any) -> str:
        if isinstance(block, dict):
            return str(block.get("id", ""))
        return str(getattr(block, "id", ""))

    @staticmethod
    def _block_input(block: Any) -> dict:
        if isinstance(block, dict):
            value = block.get("input", {})
            return value if isinstance(value, dict) else {}
        value = getattr(block, "input", {})
        return value if isinstance(value, dict) else {}

    @classmethod
    def _normalize_response_content(cls, content: Any) -> list[Any]:
        corrected: list[Any] = []
        if not isinstance(content, list):
            return corrected

        for block in content:
            if cls._block_type(block) == "tool_use":
                if isinstance(block, dict):
                    if not isinstance(block.get("input"), dict):
                        block["input"] = {}
                elif not isinstance(getattr(block, "input", None), dict):
                    block.input = {}
            corrected.append(block)
        return corrected

    def _active_research_ids(self, context: dict) -> set[str]:
        scope = str(context.get("active_memory_scope") or "lead_conversation")
        ids = self._research_runs_by_scope.setdefault(scope, set())
        if self.deps.research_run_stage is not None:
            ids.difference_update({
                item for item in ids
                if self.deps.research_run_stage(item) == "completed"
            })
        return ids

    def _pending_research(
        self, context: dict, turn_ids: set[str] | None = None
    ) -> list[tuple[str, str]]:
        if self.deps.research_run_stage is None:
            return []
        ids = self._active_research_ids(context) | (turn_ids or set())
        return [
            (item, self.deps.research_run_stage(item) or "unknown")
            for item in sorted(ids)
            if self.deps.research_run_stage(item) != "completed"
        ]

    @staticmethod
    def _research_result(output: str) -> dict:
        try:
            result = json.loads(output)
            return result if isinstance(result, dict) else {}
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _workflow_failure_message(pending: list[tuple[str, str]]) -> str:
        state = ", ".join(f"{run_id}: {stage}" for run_id, stage in pending)
        return (
            "Research workflow execution failure: the research run did not "
            f"reach completed state ({state}). No final proof was returned. "
            "Inspect the preceding workflow tool error and resume the run."
        )

    @classmethod
    def _suppress_research_prose(cls, messages: list, from_index: int) -> None:
        for message in messages[from_index:]:
            if message.get("role") == "assistant" and isinstance(message.get("content"), list):
                message["content"] = [
                    block for block in message["content"]
                    if cls._block_type(block) != "text"
                ]

    def _inject_scheduled_prompts(self, messages: list) -> None:
        for job in self.deps.claim_scheduled_prompts():
            prompt = str(job.payload.get("prompt", ""))
            messages.append({"role": "user", "content": f"[Scheduled] {prompt}"})
            self.output(f"  \033[35m[cron inject] {prompt[:60]}\033[0m")
            try:
                self.deps.mark_scheduled_prompt_delivered(job)
            except Exception as exc:
                self.output(
                    f"  \033[31m[cron delivery error] {job.id}: {exc}\033[0m"
                )

    def _append_todo_reminder_if_needed(self, messages: list) -> None:
        interval = self.config.todo_reminder_interval
        if interval > 0 and self.rounds_since_todo >= interval:
            messages.append(
                {"role": "user", "content": "<reminder>Update your todos.</reminder>"}
            )
            self.rounds_since_todo = 0

    def run(self, messages: list, context: dict) -> None:
        tracker = self.execution_tracker
        if tracker is None:
            return self._run_impl(messages, context, None, None)

        run, agent = tracker.start_run(
            agent_kind=AgentKind.LEAD,
            agent_name="lead",
            role="Lead Principal Investigator",
        )
        try:
            result = self._run_impl(messages, context, run.id, agent.id)
        except Exception as exc:
            tracker.emit(
                RuntimeEventType.ERROR,
                run_id=run.id,
                agent_id=agent.id,
                payload={"error_type": type(exc).__name__},
            )
            tracker.finish_run(
                run.id, status=ExecutionStatus.FAILED, error_type=type(exc).__name__
            )
            raise
        tracker.finish_run(
            run.id,
            status=ExecutionStatus.FAILED if result == "workflow_failed" else ExecutionStatus.SUCCEEDED,
        )
        return result

    def _run_impl(
        self,
        messages: list,
        context: dict,
        run_id: str | None,
        agent_id: str | None,
    ) -> None:
        tools, handlers = self.deps.assemble_tool_pool()
        resumed = self.deps.resume_queued_background_jobs(handlers)
        if resumed:
            self.output(
                f"  \033[33m[background] resumed {len(resumed)} queued job(s)\033[0m"
            )

        state = self.deps.make_recovery_state()
        max_tokens = self.config.default_max_tokens
        workflow_recovery_attempts = 0
        turn_research_ids: set[str] = set()
        turn_start = len(messages)

        while True:
            self._inject_scheduled_prompts(messages)
            self.deps.inject_background_notifications(messages)
            self._append_todo_reminder_if_needed(messages)

            self.deps.prepare_context(messages)
            context = self.deps.update_context(context, messages)
            tools, handlers = self.deps.assemble_tool_pool()

            try:
                model_started = time.monotonic()
                retry_before = int(getattr(state, "retry_count", 0) or 0)
                if self.execution_tracker is not None and run_id and agent_id:
                    self.execution_tracker.emit(
                        RuntimeEventType.MODEL_CALL_STARTED,
                        run_id=run_id,
                        agent_id=agent_id,
                        payload={
                            "max_tokens": max_tokens,
                            "model": str(getattr(state, "current_model", "") or ""),
                        },
                    )
                response = self.deps.call_llm(
                    messages, context, tools, state, max_tokens
                )
                if self.execution_tracker is not None and run_id and agent_id:
                    payload = response_usage_payload(response)
                    payload.update(
                        {
                            "latency_ms": round((time.monotonic() - model_started) * 1000.0, 3),
                            "retry_count": max(0, int(getattr(state, "retry_count", 0) or 0) - retry_before),
                        }
                    )
                    if not payload.get("model"):
                        payload["model"] = str(getattr(state, "current_model", "") or "")
                    self.execution_tracker.emit(
                        RuntimeEventType.MODEL_CALL_FINISHED,
                        run_id=run_id,
                        agent_id=agent_id,
                        payload=payload,
                    )
            except Exception as exc:
                if self.execution_tracker is not None and run_id and agent_id:
                    self.execution_tracker.emit(
                        RuntimeEventType.ERROR,
                        run_id=run_id,
                        agent_id=agent_id,
                        payload={"stage": "model_call", "error_type": type(exc).__name__},
                    )
                if (
                    self.deps.is_prompt_too_long_error(exc)
                    and not state.has_attempted_reactive_compact
                ):
                    messages[:] = self.deps.reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": f"[Error] {type(exc).__name__}: {exc}",
                            }
                        ],
                    }
                )
                return

            if response.stop_reason == "max_tokens":
                if not state.has_escalated:
                    max_tokens = self.config.escalated_max_tokens
                    state.has_escalated = True
                    self.output(
                        f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m"
                    )
                    continue

                messages.append({"role": "assistant", "content": response.content})
                if state.recovery_count < self.config.max_recovery_retries:
                    messages.append(
                        {"role": "user", "content": self.config.continuation_prompt}
                    )
                    state.recovery_count += 1
                    continue
                pending = self._pending_research(context, turn_research_ids)
                if pending:
                    self._suppress_research_prose(messages, turn_start)
                    messages.append({
                        "role": "assistant",
                        "content": [{"type": "text", "text": self._workflow_failure_message(pending)}],
                    })
                    return "workflow_failed"
                return

            max_tokens = self.config.default_max_tokens
            state.has_escalated = False
            corrected_content = self._normalize_response_content(response.content)

            if not self.deps.has_tool_use(response.content):
                pending = self._pending_research(context, turn_research_ids)
                if pending:
                    if workflow_recovery_attempts < 2 and all(
                        stage != "failed" for _, stage in pending
                    ):
                        workflow_recovery_attempts += 1
                        messages.append({
                            "role": "user",
                            "content": (
                                "[Runtime completion gate] Active research workflow "
                                f"is incomplete: {pending}. Continue with valid workflow "
                                "and literature tool calls."
                            ),
                        })
                        continue
                    self._suppress_research_prose(messages, turn_start)
                    messages.append({
                        "role": "assistant",
                        "content": [{"type": "text", "text": self._workflow_failure_message(pending)}],
                    })
                    self.deps.trigger_hooks("Stop", messages)
                    return "workflow_failed"
                messages.append({"role": "assistant", "content": corrected_content})
                self.deps.trigger_hooks("Stop", messages)
                return

            messages.append({"role": "assistant", "content": corrected_content})

            results: list[dict] = []
            compacted_now = False
            for block in response.content:
                if self._block_type(block) != "tool_use":
                    continue

                name = self._block_name(block)
                block_input = self._block_input(block)
                block_id = self._block_id(block)
                research_ids = self._active_research_ids(context)
                inferred_run_id = False
                if name == "research_workflow" and block_input.get("action") != "start":
                    payload = block_input.get("payload")
                    if isinstance(payload, dict) and not str(payload.get("run_id", "")).strip():
                        if len(research_ids) == 1:
                            payload["run_id"] = next(iter(research_ids))
                            inferred_run_id = True
                    elif payload is None and len(research_ids) == 1:
                        block_input["payload"] = {"run_id": next(iter(research_ids))}
                        inferred_run_id = True
                self.output(f"\033[36m> {name}\033[0m")
                tool_trace = None
                if self.execution_tracker is not None and run_id and agent_id:
                    tool_trace = self.execution_tracker.start_tool_call(
                        run_id=run_id,
                        agent_id=agent_id,
                        tool_name=name,
                        provider_tool_use_id=block_id or None,
                    )

                if name == "compact":
                    messages[:] = self.deps.compact_history(messages)
                    messages.append(
                        {
                            "role": "user",
                            "content": "[Compacted. Continue with summarized context.]",
                        }
                    )
                    compacted_now = True
                    if tool_trace is not None:
                        self.execution_tracker.finish_tool_call(
                            tool_trace.id, status=ToolCallStatus.SUCCEEDED
                        )
                    break

                blocked = self.deps.trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block_id,
                            "content": str(blocked),
                        }
                    )
                    if tool_trace is not None:
                        self.execution_tracker.finish_tool_call(
                            tool_trace.id, status=ToolCallStatus.BLOCKED
                        )
                    continue

                if self.deps.should_run_background(name, block_input):
                    bg_id = self.deps.start_background_task(block, handlers)
                    output = (
                        f"[Background task {bg_id} started] "
                        "Result will arrive as a task_notification."
                    )
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block_id,
                            "content": output,
                        }
                    )
                    if tool_trace is not None:
                        self.execution_tracker.finish_tool_call(
                            tool_trace.id,
                            status=ToolCallStatus.BACKGROUND,
                            metadata={"background_job_id": bg_id},
                        )
                    continue

                handler = handlers.get(name)
                if (
                    self.deps.record_literature_query_started is not None
                    and len(research_ids) == 1
                    and name in {"search_literature", "mcp__docs__search"}
                ):
                    self.deps.record_literature_query_started(
                        next(iter(research_ids)), name, block_input
                    )
                payload = block_input.get("payload")
                supplied_id = (
                    str(payload.get("run_id", "")).strip()
                    if isinstance(payload, dict) else ""
                )
                if (
                    name == "research_workflow"
                    and block_input.get("action") != "start"
                    and not supplied_id
                    and len(research_ids) > 1
                ):
                    output = json.dumps({
                        "ok": False,
                        "action": block_input.get("action"),
                        "error": "ambiguous active research runs; supply payload.run_id",
                    })
                else:
                    tool_started = time.monotonic()
                    output = self.deps.call_tool_handler(handler, block_input, name)
                workflow_result = self._research_result(output) if name == "research_workflow" else {}
                if supplied_id and self.deps.research_run_stage is not None:
                    if self.deps.research_run_stage(supplied_id) is not None:
                        research_ids.add(supplied_id)
                if workflow_result.get("ok"):
                    result = workflow_result.get("result")
                    # Method actions return a method id, never a research run id.
                    result_id = (
                        result.get("id")
                        if block_input.get("action") == "start" and isinstance(result, dict)
                        else None
                    )
                    resolved_id = (
                        str(result_id or supplied_id)
                        if block_input.get("action") == "start"
                        else supplied_id
                    )
                    if resolved_id:
                        research_ids.add(resolved_id)
                        turn_research_ids.add(resolved_id)
                if (
                    self.deps.record_literature_tool_result is not None
                    and len(research_ids) == 1
                    and name in {"search_literature", "mcp__docs__search"}
                ):
                    literature_input = {
                        **block_input,
                        "__latency_ms": (time.monotonic() - tool_started) * 1000,
                    }
                    self.deps.record_literature_tool_result(
                        next(iter(research_ids)), name, literature_input, str(output)
                    )
                self.deps.trigger_hooks("PostToolUse", block, output)
                self.output(str(output)[:300])
                if tool_trace is not None:
                    metadata = {"output_chars": len(str(output))}
                    if name == "research_workflow":
                        metadata.update({
                            "action": (
                                str(block_input.get("action", ""))
                                if str(block_input.get("action", "")) in {
                                    "start", "literature", "request_decomposition",
                                    "activate_method", "sync_method", "structural_verdict",
                                    "detailed_verdict", "regulator", "begin_summary",
                                    "complete_summary", "complete_direct_proof", "status",
                                }
                                else "(invalid)"
                            ),
                            "run_id_present": bool(supplied_id),
                            "run_id": (
                                supplied_id
                                if re.fullmatch(r"(?:research|benchmark)_[A-Za-z0-9_-]{1,64}", supplied_id)
                                else ""
                            ),
                            "run_id_inferred": inferred_run_id,
                        })
                    self.execution_tracker.finish_tool_call(
                        tool_trace.id,
                        status=(
                            ToolCallStatus.FAILED
                            if name == "research_workflow" and workflow_result.get("ok") is False
                            else ToolCallStatus.SUCCEEDED
                        ),
                        metadata=metadata,
                    )

                if name == "todo_write":
                    self.rounds_since_todo = 0
                else:
                    self.rounds_since_todo += 1

                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block_id,
                        "content": output,
                    }
                )

            if compacted_now:
                continue

            messages.append(
                {"role": "user", "content": self.deps.build_user_content(results)}
            )
