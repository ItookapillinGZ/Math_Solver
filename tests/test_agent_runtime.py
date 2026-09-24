import unittest
from types import SimpleNamespace

from agent_runtime.runtime import (
    AgentRuntime,
    AgentRuntimeConfig,
    AgentRuntimeDependencies,
)


class FakeBlock:
    def __init__(self, block_type, *, name="", tool_input=None, block_id="tool_1", text=""):
        self.type = block_type
        self.name = name
        self.input = {} if tool_input is None else tool_input
        self.id = block_id
        self.text = text


class FakeResponse:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class FakeState:
    def __init__(self):
        self.has_escalated = False
        self.recovery_count = 0
        self.has_attempted_reactive_compact = False
        self.current_model = "test-model"


class RuntimeHarness:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.call_tokens = []
        self.hook_events = []
        self.handler_calls = []
        self.background_calls = []
        self.resume_calls = 0
        self.scheduled_batches = []
        self.delivered_jobs = []
        self.reactive_calls = 0
        self.compact_calls = 0
        self.outputs = []
        self.handlers = {"echo": self.echo_handler}
        self.block_pre_tool = False
        self.force_background = False
        self.research_run_stage = None
        self.record_literature_tool_result = None

    def echo_handler(self, value=""):
        self.handler_calls.append(value)
        return f"echo:{value}"

    def assemble_tool_pool(self):
        return ([{"name": "echo"}], dict(self.handlers))

    def resume_background(self, handlers):
        self.resume_calls += 1
        return []

    def claim_scheduled(self):
        if self.scheduled_batches:
            return self.scheduled_batches.pop(0)
        return []

    def mark_delivered(self, job):
        self.delivered_jobs.append(job.id)

    def inject_background(self, messages):
        return None

    def prepare_context(self, messages):
        return messages

    def update_context(self, context, messages):
        return dict(context)

    def call_llm(self, messages, context, tools, state, max_tokens):
        self.call_tokens.append(max_tokens)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @staticmethod
    def is_prompt_too_long(exc):
        return "too long" in str(exc).lower()

    def reactive_compact(self, messages):
        self.reactive_calls += 1
        return [{"role": "user", "content": "reactive checkpoint"}]

    @staticmethod
    def has_tool_use(content):
        return any(getattr(block, "type", None) == "tool_use" for block in content)

    def trigger_hooks(self, event, *args):
        self.hook_events.append(event)
        if event == "PreToolUse" and self.block_pre_tool:
            return "blocked by policy"
        return None

    def compact_history(self, messages):
        self.compact_calls += 1
        return [{"role": "user", "content": "structured checkpoint"}]

    def should_background(self, name, tool_input):
        return self.force_background

    def start_background(self, block, handlers):
        self.background_calls.append(block.name)
        return "job_bg_1"

    @staticmethod
    def call_tool_handler(handler, args, name):
        if handler is None:
            return f"Unknown: {name}"
        return handler(**args)

    @staticmethod
    def build_user_content(results):
        return list(results)

    def runtime(self):
        deps = AgentRuntimeDependencies(
            assemble_tool_pool=self.assemble_tool_pool,
            resume_queued_background_jobs=self.resume_background,
            claim_scheduled_prompts=self.claim_scheduled,
            mark_scheduled_prompt_delivered=self.mark_delivered,
            inject_background_notifications=self.inject_background,
            prepare_context=self.prepare_context,
            update_context=self.update_context,
            call_llm=self.call_llm,
            make_recovery_state=FakeState,
            is_prompt_too_long_error=self.is_prompt_too_long,
            reactive_compact=self.reactive_compact,
            has_tool_use=self.has_tool_use,
            trigger_hooks=self.trigger_hooks,
            compact_history=self.compact_history,
            should_run_background=self.should_background,
            start_background_task=self.start_background,
            call_tool_handler=self.call_tool_handler,
            build_user_content=self.build_user_content,
            research_run_stage=self.research_run_stage,
            record_literature_tool_result=self.record_literature_tool_result,
        )
        return AgentRuntime(
            AgentRuntimeConfig(
                default_max_tokens=100,
                escalated_max_tokens=200,
                max_recovery_retries=1,
                continuation_prompt="continue",
                todo_reminder_interval=3,
            ),
            deps,
            output=self.outputs.append,
        )


class AgentRuntimeTests(unittest.TestCase):
    def test_plain_response_stops_and_triggers_stop_hook(self):
        h = RuntimeHarness([FakeResponse([FakeBlock("text", text="done")])])
        runtime = h.runtime()
        messages = [{"role": "user", "content": "hello"}]

        runtime.run(messages, {})

        self.assertIn("Stop", h.hook_events)
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertEqual(h.call_tokens, [100])

    def test_tool_call_executes_handler_and_feeds_result_back(self):
        h = RuntimeHarness([
            FakeResponse([FakeBlock("tool_use", name="echo", tool_input={"value": "x"})]),
            FakeResponse([FakeBlock("text", text="done")]),
        ])
        runtime = h.runtime()
        messages = [{"role": "user", "content": "use tool"}]

        runtime.run(messages, {})

        self.assertEqual(h.handler_calls, ["x"])
        tool_result_messages = [
            msg for msg in messages
            if msg.get("role") == "user" and isinstance(msg.get("content"), list)
        ]
        self.assertTrue(any(
            item.get("content") == "echo:x"
            for msg in tool_result_messages
            for item in msg["content"]
            if isinstance(item, dict) and item.get("type") == "tool_result"
        ))

    def test_pre_tool_hook_can_block_execution(self):
        h = RuntimeHarness([
            FakeResponse([FakeBlock("tool_use", name="echo", tool_input={"value": "x"})]),
            FakeResponse([FakeBlock("text", text="done")]),
        ])
        h.block_pre_tool = True
        runtime = h.runtime()
        messages = [{"role": "user", "content": "use tool"}]

        runtime.run(messages, {})

        self.assertEqual(h.handler_calls, [])
        self.assertIn("PreToolUse", h.hook_events)
        self.assertTrue(any(
            item.get("content") == "blocked by policy"
            for msg in messages
            if msg.get("role") == "user" and isinstance(msg.get("content"), list)
            for item in msg["content"]
            if isinstance(item, dict)
        ))

    def test_background_tool_is_started_without_inline_handler_execution(self):
        h = RuntimeHarness([
            FakeResponse([FakeBlock("tool_use", name="echo", tool_input={"value": "x"})]),
            FakeResponse([FakeBlock("text", text="done")]),
        ])
        h.force_background = True
        runtime = h.runtime()
        messages = [{"role": "user", "content": "background"}]

        runtime.run(messages, {})

        self.assertEqual(h.background_calls, ["echo"])
        self.assertEqual(h.handler_calls, [])

    def test_prompt_too_long_uses_reactive_compaction_once(self):
        h = RuntimeHarness([
            RuntimeError("prompt too long"),
            FakeResponse([FakeBlock("text", text="done")]),
        ])
        runtime = h.runtime()
        messages = [{"role": "user", "content": "large"}]

        runtime.run(messages, {})

        self.assertEqual(h.reactive_calls, 1)
        self.assertEqual(h.call_tokens, [100, 100])
        self.assertEqual(messages[0]["content"], "reactive checkpoint")

    def test_max_tokens_escalates_then_uses_continuation(self):
        h = RuntimeHarness([
            FakeResponse([FakeBlock("text", text="partial-1")], stop_reason="max_tokens"),
            FakeResponse([FakeBlock("text", text="partial-2")], stop_reason="max_tokens"),
            FakeResponse([FakeBlock("text", text="done")]),
        ])
        runtime = h.runtime()
        messages = [{"role": "user", "content": "long answer"}]

        runtime.run(messages, {})

        self.assertEqual(h.call_tokens, [100, 200, 200])
        self.assertTrue(any(msg.get("content") == "continue" for msg in messages))

    def test_compact_control_tool_uses_compaction_callback(self):
        h = RuntimeHarness([
            FakeResponse([FakeBlock("tool_use", name="compact")]),
            FakeResponse([FakeBlock("text", text="done")]),
        ])
        runtime = h.runtime()
        messages = [{"role": "user", "content": "compact"}]

        runtime.run(messages, {})

        self.assertEqual(h.compact_calls, 1)
        self.assertTrue(any(
            msg.get("content") == "[Compacted. Continue with summarized context.]"
            for msg in messages
        ))

    def test_scheduled_prompt_is_injected_and_marked_delivered(self):
        h = RuntimeHarness([FakeResponse([FakeBlock("text", text="done")])])
        h.scheduled_batches = [[SimpleNamespace(id="cron_job_1", payload={"prompt": "check proof"})]]
        runtime = h.runtime()
        messages = []

        runtime.run(messages, {})

        self.assertEqual(h.delivered_jobs, ["cron_job_1"])
        self.assertTrue(any(msg.get("content") == "[Scheduled] check proof" for msg in messages))

    def test_todo_reminder_state_lives_in_runtime_instance(self):
        h = RuntimeHarness([FakeResponse([FakeBlock("text", text="done")])])
        runtime = h.runtime()
        runtime.rounds_since_todo = 3
        messages = []

        runtime.run(messages, {})

        self.assertTrue(any(
            msg.get("content") == "<reminder>Update your todos.</reminder>"
            for msg in messages
        ))
        self.assertEqual(runtime.rounds_since_todo, 0)


if __name__ == "__main__":
    unittest.main()
