import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.runtime import (
    AgentKind,
    AgentRuntime,
    AgentRuntimeConfig,
    AgentRuntimeDependencies,
    ExecutionStatus,
    ExecutionTracker,
    RuntimeEventType,
    SubagentRuntime,
    SubagentRuntimeConfig,
    SubagentRuntimeDependencies,
    TeammateRuntime,
    TeammateRuntimeDependencies,
    ToolCallStatus,
)


class IdFactory:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n}"


def make_tracker(max_events=50, sink=None):
    return ExecutionTracker(
        id_factory=IdFactory(),
        clock=lambda: "2026-09-20T12:00:00+00:00",
        max_events=max_events,
        event_sink=sink,
    )


class TextBlock:
    type = "text"

    def __init__(self, text="done"):
        self.text = text


class ToolBlock:
    type = "tool_use"

    def __init__(self, name, input_=None, block_id="provider_tool_1"):
        self.name = name
        self.input = dict(input_ or {})
        self.id = block_id


class Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class RecoveryState:
    has_escalated = False
    recovery_count = 0
    has_attempted_reactive_compact = False


class ExecutionModelTests(unittest.TestCase):
    def test_start_and_finish_run_create_agent_and_events(self):
        tracker = make_tracker()
        run, agent = tracker.start_run(
            agent_kind=AgentKind.LEAD,
            agent_name="lead",
            role="PI",
        )

        finished = tracker.finish_run(run.id)

        self.assertEqual(agent.kind, AgentKind.LEAD)
        self.assertEqual(finished.status, ExecutionStatus.SUCCEEDED)
        self.assertIsNotNone(finished.ended_at)
        self.assertEqual(
            [event.type for event in tracker.list_events()],
            [
                RuntimeEventType.AGENT_STARTED,
                RuntimeEventType.RUN_STARTED,
                RuntimeEventType.RUN_FINISHED,
                RuntimeEventType.AGENT_FINISHED,
            ],
        )

    def test_records_serialize_enums_as_plain_values(self):
        tracker = make_tracker()
        run, agent = tracker.start_run(
            agent_kind="subagent", agent_name="subagent", role="focused"
        )
        tool = tracker.start_tool_call(
            run_id=run.id,
            agent_id=agent.id,
            tool_name="read_file",
        )
        tracker.finish_tool_call(tool.id, status="blocked")

        self.assertEqual(agent.to_dict()["kind"], "subagent")
        self.assertEqual(tracker.get_run(run.id).to_dict()["status"], "running")
        self.assertEqual(tracker.get_tool_call(tool.id).to_dict()["status"], "blocked")
        self.assertEqual(tracker.list_events()[-1].to_dict()["type"], "tool_call_finished")

    def test_tool_call_lifecycle_records_only_safe_metadata(self):
        tracker = make_tracker()
        run, agent = tracker.start_run(agent_kind="lead", agent_name="lead")
        tool = tracker.start_tool_call(
            run_id=run.id,
            agent_id=agent.id,
            tool_name="bash",
            provider_tool_use_id="abc",
        )
        finished = tracker.finish_tool_call(
            tool.id,
            status=ToolCallStatus.SUCCEEDED,
            metadata={"output_chars": 12},
        )

        payload = repr(finished.to_dict()) + repr([e.to_dict() for e in tracker.list_events()])
        self.assertEqual(finished.metadata["output_chars"], 12)
        self.assertNotIn("super-secret-command", payload)
        self.assertNotIn("input", finished.to_dict())

    def test_nested_runs_inherit_parent_context_on_same_thread(self):
        tracker = make_tracker()
        parent_run, parent_agent = tracker.start_run(
            agent_kind="lead", agent_name="lead"
        )
        child_run, child_agent = tracker.start_run(
            agent_kind="subagent", agent_name="subagent"
        )

        self.assertEqual(child_run.parent_run_id, parent_run.id)
        self.assertEqual(child_agent.parent_agent_id, parent_agent.id)
        self.assertEqual(tracker.capture_parent(), (child_run.id, child_agent.id))

        tracker.finish_run(child_run.id)
        self.assertEqual(tracker.capture_parent(), (parent_run.id, parent_agent.id))
        tracker.finish_run(parent_run.id)
        self.assertEqual(tracker.capture_parent(), (None, None))

    def test_event_buffer_is_bounded(self):
        tracker = make_tracker(max_events=3)
        run, agent = tracker.start_run(agent_kind="lead", agent_name="lead")
        for index in range(5):
            tracker.emit(
                RuntimeEventType.MODEL_CALL_STARTED,
                run_id=run.id,
                agent_id=agent.id,
                payload={"index": index},
            )

        events = tracker.list_events()
        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1].payload["index"], 4)

    def test_event_sink_failure_never_changes_runtime_state(self):
        def broken_sink(_event):
            raise OSError("sink unavailable")

        tracker = make_tracker(sink=broken_sink)
        run, _ = tracker.start_run(agent_kind="lead", agent_name="lead")
        finished = tracker.finish_run(run.id)

        self.assertEqual(finished.status, ExecutionStatus.SUCCEEDED)

    def test_lead_runtime_emits_run_model_and_tool_records(self):
        tracker = make_tracker()
        responses = [
            Response([ToolBlock("echo", {"value": "secret-value"})]),
            Response([TextBlock("done")]),
        ]

        def call_llm(*_args):
            return responses.pop(0)

        deps = AgentRuntimeDependencies(
            assemble_tool_pool=lambda: ([{"name": "echo"}], {"echo": lambda value="": f"echo:{value}"}),
            resume_queued_background_jobs=lambda handlers: [],
            claim_scheduled_prompts=lambda: [],
            mark_scheduled_prompt_delivered=lambda job: None,
            inject_background_notifications=lambda messages: None,
            prepare_context=lambda messages: messages,
            update_context=lambda context, messages: context,
            call_llm=call_llm,
            make_recovery_state=RecoveryState,
            is_prompt_too_long_error=lambda exc: False,
            reactive_compact=lambda messages: messages,
            has_tool_use=lambda content: any(getattr(b, "type", None) == "tool_use" for b in content),
            trigger_hooks=lambda event, *args: None,
            compact_history=lambda messages: messages,
            should_run_background=lambda name, args: False,
            start_background_task=lambda block, handlers: "job_1",
            call_tool_handler=lambda handler, args, name: handler(**args),
            build_user_content=lambda results: results,
        )
        runtime = AgentRuntime(
            AgentRuntimeConfig(
                default_max_tokens=100,
                escalated_max_tokens=200,
                max_recovery_retries=1,
                continuation_prompt="continue",
            ),
            deps,
            output=lambda _: None,
            execution_tracker=tracker,
        )

        runtime.run([{"role": "user", "content": "hello"}], {})

        self.assertEqual(tracker.list_runs()[0].status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(tracker.list_agents()[0].kind, AgentKind.LEAD)
        calls = tracker.list_tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].tool_name, "echo")
        self.assertEqual(calls[0].status, ToolCallStatus.SUCCEEDED)
        self.assertNotIn("secret-value", repr([e.to_dict() for e in tracker.list_events()]))

    def test_lead_runtime_marks_blocked_tool_call(self):
        tracker = make_tracker()
        responses = [
            Response([ToolBlock("echo", {"value": "x"})]),
            Response([TextBlock("done")]),
        ]
        deps = AgentRuntimeDependencies(
            assemble_tool_pool=lambda: ([{"name": "echo"}], {"echo": lambda value="": value}),
            resume_queued_background_jobs=lambda handlers: [],
            claim_scheduled_prompts=lambda: [],
            mark_scheduled_prompt_delivered=lambda job: None,
            inject_background_notifications=lambda messages: None,
            prepare_context=lambda messages: messages,
            update_context=lambda context, messages: context,
            call_llm=lambda *_args: responses.pop(0),
            make_recovery_state=RecoveryState,
            is_prompt_too_long_error=lambda exc: False,
            reactive_compact=lambda messages: messages,
            has_tool_use=lambda content: any(getattr(b, "type", None) == "tool_use" for b in content),
            trigger_hooks=lambda event, *args: "blocked" if event == "PreToolUse" else None,
            compact_history=lambda messages: messages,
            should_run_background=lambda name, args: False,
            start_background_task=lambda block, handlers: "job_1",
            call_tool_handler=lambda handler, args, name: handler(**args),
            build_user_content=lambda results: results,
        )
        runtime = AgentRuntime(
            AgentRuntimeConfig(100, 200, 1, "continue"),
            deps,
            output=lambda _: None,
            execution_tracker=tracker,
        )

        runtime.run([{"role": "user", "content": "hello"}], {})

        self.assertEqual(tracker.list_tool_calls()[0].status, ToolCallStatus.BLOCKED)

    def test_subagent_runtime_uses_same_execution_models(self):
        tracker = make_tracker()
        runtime = SubagentRuntime(
            SubagentRuntimeConfig(workspace=Path("/tmp"), max_steps=1, max_tokens=50),
            SubagentRuntimeDependencies(
                create_message=lambda **kwargs: Response([TextBlock("summary")]),
                trigger_hooks=lambda event, *args: None,
                call_tool_handler=lambda handler, args, name: handler(**args),
                run_bash=lambda command: command,
                run_read=lambda path, limit=None, offset=0: path,
                run_write=lambda path, content: content,
                run_edit=lambda path, old_text, new_text: new_text,
                run_glob=lambda pattern: pattern,
            ),
            execution_tracker=tracker,
        )

        result = runtime.run("inspect")

        self.assertEqual(result, "summary")
        self.assertEqual(tracker.list_agents()[0].kind, AgentKind.SUBAGENT)
        self.assertEqual(tracker.list_runs()[0].status, ExecutionStatus.SUCCEEDED)

    def test_teammate_runtime_uses_same_execution_models(self):
        tracker = make_tracker()

        class Bus:
            def __init__(self):
                self.sent = []
            def read_inbox(self, agent):
                return []
            def send(self, *args):
                self.sent.append(args)

        bus = Bus()
        coordinator = SimpleNamespace(bus=bus, submit_plan=lambda name, plan: "Plan submitted (req_1)")
        application = SimpleNamespace(
            build_teammate_system_prompt=lambda name, role: "system",
            build_identity_prompt=lambda name, role: "identity",
            progress_nudge="continue",
        )
        thread_targets = []
        deps = TeammateRuntimeDependencies(
            application=application,
            coordinator=coordinator,
            list_tasks=lambda: [],
            load_task=lambda task_id: (_ for _ in ()).throw(FileNotFoundError(task_id)),
            can_start=lambda task_id: True,
            claim_task=lambda task_id, owner="agent": "Cannot claim",
            complete_task=lambda task_id: "Completed",
            recover_expired_task_leases=lambda log=False: [],
            task_matches_worker=lambda task, role: True,
            select_tasks_for_worker=lambda tasks, role: tasks,
            heartbeat_task=lambda task_id, owner="agent": None,
            worktrees_dir=Path("/tmp/worktrees"),
            run_bash=lambda command, cwd=None: command,
            run_read=lambda path, limit=None, offset=0, cwd=None: path,
            run_write=lambda path, content, cwd=None: content,
            call_tool_handler=lambda handler, args, name: handler(**args) if handler else "unknown",
            has_tool_use=lambda content: False,
            create_message=lambda **kwargs: Response([TextBlock("TASK FINISHED")]),
            terminal_print=lambda text: None,
            idle_poll_interval=1,
            idle_timeout=1,
            sleep=lambda _: None,
            monotonic=lambda: 1.0,
            start_thread=thread_targets.append,
        )
        runtime = TeammateRuntime(deps, execution_tracker=tracker)
        runtime.spawn("proof_worker", "mathematical researcher", "work")
        thread_targets[0]()

        self.assertEqual(tracker.list_agents()[0].kind, AgentKind.TEAMMATE)
        self.assertEqual(tracker.list_agents()[0].name, "proof_worker")
        self.assertEqual(tracker.list_runs()[0].status, ExecutionStatus.SUCCEEDED)

    def test_failed_subagent_run_is_marked_failed_and_reraises(self):
        tracker = make_tracker()
        runtime = SubagentRuntime(
            SubagentRuntimeConfig(workspace=Path("/tmp"), max_steps=1, max_tokens=50),
            SubagentRuntimeDependencies(
                create_message=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
                trigger_hooks=lambda event, *args: None,
                call_tool_handler=lambda handler, args, name: "",
                run_bash=lambda command: "",
                run_read=lambda path, limit=None, offset=0: "",
                run_write=lambda path, content: "",
                run_edit=lambda path, old_text, new_text: "",
                run_glob=lambda pattern: "",
            ),
            execution_tracker=tracker,
        )

        with self.assertRaises(RuntimeError):
            runtime.run("fail")

        self.assertEqual(tracker.list_runs()[0].status, ExecutionStatus.FAILED)
        self.assertEqual(tracker.list_runs()[0].error_type, "RuntimeError")


if __name__ == "__main__":
    unittest.main()
