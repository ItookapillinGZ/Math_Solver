import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from agent_runtime.runtime.teammate import (
    TeammateRuntime,
    TeammateRuntimeDependencies,
)


@dataclass
class FakeTask:
    id: str
    subject: str
    status: str = "pending"
    owner: str | None = None
    task_type: str = "math"
    required_roles: list[str] = field(default_factory=list)
    worktree: str | None = None
    description: str = ""
    tags: list[str] = field(default_factory=list)


class FakeBus:
    def __init__(self):
        self.inboxes = {}
        self.sent = []

    def read_inbox(self, agent):
        queue = self.inboxes.get(agent, [])
        if not queue:
            return []
        self.inboxes[agent] = []
        return list(queue)

    def send(self, from_agent, to_agent, content, msg_type="message", metadata=None):
        self.sent.append((from_agent, to_agent, content, msg_type, metadata or {}))


class FakeCoordinator:
    def __init__(self):
        self.bus = FakeBus()
        self.submitted = []

    def submit_plan(self, from_name, plan):
        self.submitted.append((from_name, plan))
        self.bus.inboxes.setdefault(from_name, []).append({
            "type": "plan_approval_response",
            "content": "Approved",
            "metadata": {"request_id": "req_1", "approve": True},
        })
        return "Plan submitted (req_1)"


class FakeApplication:
    progress_nudge = "continue"

    def build_teammate_system_prompt(self, name, role):
        return f"SYSTEM:{name}:{role}"

    def build_identity_prompt(self, name, role):
        return f"IDENTITY:{name}:{role}"


class FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeToolBlock:
    type = "tool_use"

    def __init__(self, name, input_, block_id="tool_1"):
        self.name = name
        self.input = input_
        self.id = block_id


class FakeResponse:
    def __init__(self, content):
        self.content = content


class RuntimeFixture:
    def __init__(self):
        self.tasks = []
        self.coordinator = FakeCoordinator()
        self.claimed = []
        self.completed = []
        self.recovery_calls = 0
        self.heartbeat_calls = []
        self.model_calls = []
        self.responses = [FakeResponse([FakeTextBlock("TASK FINISHED")])]
        self.thread_targets = []
        self.read_calls = []
        self.worktrees_dir = Path("/tmp/worktrees")

    def list_tasks(self):
        return self.tasks

    def load_task(self, task_id):
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise FileNotFoundError(task_id)

    def can_start(self, task_id):
        return task_id != "blocked"

    def claim_task(self, task_id, owner="agent"):
        task = self.load_task(task_id)
        if task.status != "pending" or task.owner:
            return "Cannot claim"
        task.owner = owner
        task.status = "in_progress"
        self.claimed.append((task_id, owner))
        return f"Claimed {task_id} ({task.subject})"

    def complete_task(self, task_id, owner="agent"):
        task = self.load_task(task_id)
        if task.owner != owner:
            raise PermissionError(f"Task {task_id} is owned by {task.owner}, not {owner}")
        task.status = "completed"
        self.completed.append(task_id)
        return f"Completed {task_id} ({task.subject})"

    def recover(self, log=True):
        self.recovery_calls += 1
        return []

    @staticmethod
    def matches(task, role):
        if task.required_roles:
            return role in task.required_roles
        return task.task_type == "math" and "mathematical" in role.lower()

    def select(self, tasks, role):
        return [task for task in tasks if self.matches(task, role)]

    def heartbeat(self, task_id, owner="agent"):
        self.heartbeat_calls.append((task_id, owner))
        return self.load_task(task_id)

    @staticmethod
    def run_bash(command, cwd=None):
        return f"bash:{command}:{cwd}"

    def run_read(self, path, limit=None, offset=0, cwd=None):
        self.read_calls.append((path, limit, offset, cwd))
        return "read"

    def run_write(self, path, content, cwd=None):
        if cwd is not None:
            target = cwd / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return f"write:{path}:{cwd}"

    @staticmethod
    def call_handler(handler, args, name):
        if handler is None:
            return f"Unknown: {name}"
        return handler(**(args or {}))

    @staticmethod
    def has_tool_use(content):
        return any(getattr(block, "type", None) == "tool_use" for block in content)

    def create_message(self, **kwargs):
        self.model_calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected model call")
        return self.responses.pop(0)

    def start_thread(self, target):
        self.thread_targets.append(target)

    def runtime(self):
        deps = TeammateRuntimeDependencies(
            application=FakeApplication(),
            coordinator=self.coordinator,
            list_tasks=self.list_tasks,
            load_task=self.load_task,
            can_start=self.can_start,
            claim_task=self.claim_task,
            complete_task=self.complete_task,
            recover_expired_task_leases=self.recover,
            task_matches_worker=self.matches,
            select_tasks_for_worker=self.select,
            heartbeat_task=self.heartbeat,
            worktrees_dir=self.worktrees_dir,
            run_bash=self.run_bash,
            run_read=self.run_read,
            run_write=self.run_write,
            call_tool_handler=self.call_handler,
            has_tool_use=self.has_tool_use,
            create_message=self.create_message,
            terminal_print=lambda text: None,
            heartbeat_interval=60,
            idle_poll_interval=1,
            idle_timeout=1,
            sleep=lambda _: None,
            monotonic=lambda: 100.0,
            start_thread=self.start_thread,
        )
        return TeammateRuntime(deps)


class TeammateRuntimeTests(unittest.TestCase):
    def test_scan_unclaimed_tasks_filters_by_state_dependencies_and_affinity(self):
        fx = RuntimeFixture()
        fx.tasks = [
            FakeTask("math", "prove lemma"),
            FakeTask("engineering", "registry migration", task_type="engineering"),
            FakeTask("blocked", "blocked lemma"),
            FakeTask("owned", "owned lemma", owner="someone"),
        ]
        runtime = fx.runtime()

        result = runtime.scan_unclaimed_tasks("mathematical researcher")

        self.assertEqual([task.id for task in result], ["math"])
        self.assertEqual(fx.recovery_calls, 1)

    def test_idle_poll_prioritizes_inbox_over_available_task(self):
        fx = RuntimeFixture()
        fx.tasks = [FakeTask("math", "prove lemma")]
        fx.coordinator.bus.inboxes["worker"] = [
            {"type": "message", "content": "Please inspect Lemma 2", "metadata": {}}
        ]
        runtime = fx.runtime()
        messages = []

        outcome = runtime.idle_poll(
            "worker", messages, "mathematical researcher"
        )

        self.assertEqual(outcome, "work")
        self.assertEqual(fx.claimed, [])
        self.assertIn("Please inspect Lemma 2", messages[-1]["content"])

    def test_idle_poll_auto_claims_matching_task_and_records_worktree(self):
        fx = RuntimeFixture()
        fx.tasks = [FakeTask("math", "prove lemma", worktree="proof-a")]
        runtime = fx.runtime()
        messages = []
        claimed = []

        outcome = runtime.idle_poll(
            "worker",
            messages,
            "mathematical researcher",
            on_claim=claimed.append,
        )

        self.assertEqual(outcome, "work")
        self.assertEqual(claimed, ["math"])
        self.assertEqual(fx.claimed, [("math", "worker")])
        self.assertIn("prove lemma", messages[-1]["content"])
        self.assertIn("proof-a", messages[-1]["content"])

    def test_research_task_completion_requires_prover_proof_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = RuntimeFixture()
            fx.worktrees_dir = Path(tmp)
            fx.tasks = [FakeTask(
                "step_1",
                "prove S1",
                required_roles=["mathematical researcher"],
                worktree="proof-tree",
                description="Cumulative proof file: PROOF/run/method/proof.md",
                tags=["research_workflow", "step:S1"],
            )]
            fx.responses = [
                FakeResponse([
                    FakeToolBlock("claim_task", {"task_id": "step_1"}, "claim"),
                    FakeToolBlock("complete_task", {"task_id": "step_1"}, "early"),
                    FakeToolBlock(
                        "write_file",
                        {"path": "PROOF/run/method/proof.md", "content": "S1 proof"},
                        "write",
                    ),
                    FakeToolBlock("complete_task", {"task_id": "step_1"}, "done"),
                ]),
                FakeResponse([FakeTextBlock("TASK FINISHED")]),
            ]
            runtime = fx.runtime()
            runtime.spawn("prover", "mathematical researcher", "work")
            fx.thread_targets[0]()

            self.assertEqual(fx.claimed, [("step_1", "prover")])
            self.assertEqual(fx.completed, ["step_1"])
            results = fx.model_calls[1]["messages"]
            flattened = repr(results)
            self.assertIn("cumulative proof file does not exist", flattened)
            self.assertIn("Completed step_1", flattened)
            self.assertEqual(
                (fx.worktrees_dir / "proof-tree/PROOF/run/method/proof.md").read_text(
                    encoding="utf-8"
                ),
                "S1 proof",
            )

    def test_existing_proof_must_change_after_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = RuntimeFixture()
            fx.worktrees_dir = Path(tmp)
            proof = fx.worktrees_dir / "proof-tree/PROOF/run/method/proof.md"
            proof.parent.mkdir(parents=True)
            proof.write_text("S1 proof", encoding="utf-8")
            fx.tasks = [FakeTask(
                "step_2",
                "prove S2",
                required_roles=["mathematical researcher"],
                worktree="proof-tree",
                description="Cumulative proof file: PROOF/run/method/proof.md",
                tags=["research_workflow", "step:S2"],
            )]
            fx.responses = [
                FakeResponse([
                    FakeToolBlock("claim_task", {"task_id": "step_2"}, "claim"),
                    FakeToolBlock("complete_task", {"task_id": "step_2"}, "early"),
                    FakeToolBlock(
                        "write_file",
                        {
                            "path": "PROOF/run/method/proof.md",
                            "content": "S1 proof\nS2 proof",
                        },
                        "write",
                    ),
                    FakeToolBlock("complete_task", {"task_id": "step_2"}, "done"),
                ]),
                FakeResponse([FakeTextBlock("TASK FINISHED")]),
            ]
            runtime = fx.runtime()
            runtime.spawn("prover", "mathematical researcher", "work")
            fx.thread_targets[0]()

            self.assertEqual(fx.completed, ["step_2"])
            self.assertIn(
                "update the cumulative proof file after claiming",
                repr(fx.model_calls[1]["messages"]),
            )
            self.assertEqual(proof.read_text(encoding="utf-8"), "S1 proof\nS2 proof")

    def test_spawn_registers_worker_and_rejects_duplicate(self):
        fx = RuntimeFixture()
        runtime = fx.runtime()

        first = runtime.spawn("proof_worker", "mathematical researcher", "work")
        second = runtime.spawn("proof_worker", "mathematical researcher", "work")

        self.assertIn("spawned", first)
        self.assertIn("already exists", second)
        self.assertIn("proof_worker", runtime.active_teammates)
        self.assertEqual(len(fx.thread_targets), 1)

    def test_finished_worker_reports_result_and_is_removed_from_active_set(self):
        fx = RuntimeFixture()
        runtime = fx.runtime()
        runtime.spawn("proof_worker", "mathematical researcher", "work")

        fx.thread_targets[0]()

        self.assertNotIn("proof_worker", runtime.active_teammates)
        result_messages = [msg for msg in fx.coordinator.bus.sent if msg[3] == "result"]
        self.assertEqual(len(result_messages), 1)
        self.assertEqual(result_messages[0][2], "TASK FINISHED")
        self.assertEqual(fx.model_calls[0]["system"], "SYSTEM:proof_worker:mathematical researcher")

    def test_submit_plan_waits_for_protocol_response_before_next_model_step(self):
        fx = RuntimeFixture()
        fx.responses = [
            FakeResponse([FakeToolBlock("submit_plan", {"plan": "prove in two steps"})]),
            FakeResponse([FakeTextBlock("TASK FINISHED")]),
        ]
        runtime = fx.runtime()
        runtime.spawn("proof_worker", "mathematical researcher", "work")

        fx.thread_targets[0]()

        self.assertEqual(fx.coordinator.submitted, [("proof_worker", "prove in two steps")])
        self.assertEqual(len(fx.model_calls), 2)
        second_messages = fx.model_calls[1]["messages"]
        flattened = repr(second_messages)
        self.assertIn("[Plan approved]", flattened)

    def test_plan_rejection_is_added_to_worker_context(self):
        fx = RuntimeFixture()
        runtime = fx.runtime()
        messages = []
        protocol = {"waiting_plan": "req_7"}

        stopped = runtime._handle_inbox_message(
            name="proof_worker",
            msg={
                "type": "plan_approval_response",
                "content": "Fix Step 2",
                "metadata": {"request_id": "req_7", "approve": False},
            },
            messages=messages,
            protocol_ctx=protocol,
        )

        self.assertFalse(stopped)
        self.assertIsNone(protocol["waiting_plan"])
        self.assertEqual(messages[-1]["content"], "[Plan rejected] Fix Step 2")

    def test_sanitize_tool_inputs_repairs_json_string_and_empty_list(self):
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "tool_use", "input": '{"task_id":"task_1"}'},
                {"type": "tool_use", "input": []},
            ],
        }]

        TeammateRuntime._sanitize_tool_inputs(messages)

        self.assertEqual(messages[0]["content"][0]["input"], {"task_id": "task_1"})
        self.assertEqual(messages[0]["content"][1]["input"], {})

    def test_permanent_client_error_is_not_retryable(self):
        self.assertTrue(TeammateRuntime._is_permanent_model_error(
            RuntimeError("Error code: 400 - organization_on_hold")
        ))
        self.assertFalse(TeammateRuntime._is_permanent_model_error(
            RuntimeError("Error code: 429 - rate limit")
        ))

    def test_permanent_model_error_stops_after_one_call_and_notifies_lead(self):
        fx = RuntimeFixture()
        calls = []

        def fail_once(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("Error code: 400 - organization_on_hold")

        fx.create_message = fail_once
        runtime = fx.runtime()
        runtime.spawn("proof_worker", "mathematical researcher", "work")
        with self.assertRaisesRegex(RuntimeError, "organization_on_hold"):
            fx.thread_targets[0]()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("proof_worker", runtime.active_teammates)
        self.assertTrue(any(
            sent[1] == "lead" and sent[3] == "result"
            and "permanent model error" in sent[2]
            for sent in fx.coordinator.bus.sent
        ))

    def test_shutdown_request_returns_response_without_model_work(self):
        fx = RuntimeFixture()
        runtime = fx.runtime()
        messages = []
        protocol = {"waiting_plan": None}

        stopped = runtime._handle_inbox_message(
            name="proof_worker",
            msg={
                "type": "shutdown_request",
                "content": "stop",
                "metadata": {"request_id": "req_9"},
            },
            messages=messages,
            protocol_ctx=protocol,
        )

        self.assertTrue(stopped)
        self.assertEqual(fx.coordinator.bus.sent[-1][3], "shutdown_response")
        self.assertEqual(fx.coordinator.bus.sent[-1][4]["request_id"], "req_9")


if __name__ == "__main__":
    unittest.main()
