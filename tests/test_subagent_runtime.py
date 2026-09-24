import copy
import unittest
from pathlib import Path

from agent_runtime.runtime.subagent import (
    SUBAGENT_TOOLS,
    SubagentRuntime,
    SubagentRuntimeConfig,
    SubagentRuntimeDependencies,
)


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


class Fixture:
    def __init__(self):
        self.responses = []
        self.model_calls = []
        self.hook_calls = []
        self.handler_calls = []
        self.blocked = None

    def create_message(self, **kwargs):
        self.model_calls.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("unexpected model call")
        return self.responses.pop(0)

    def trigger_hooks(self, event, *args):
        self.hook_calls.append((event, args))
        if event == "PreToolUse":
            return self.blocked
        return None

    def call_tool_handler(self, handler, args, name):
        self.handler_calls.append((handler, args, name))
        if handler is None:
            return f"Unknown tool: {name}"
        return handler(**(args or {}))

    @staticmethod
    def run_bash(command):
        return f"bash:{command}"

    @staticmethod
    def run_read(path, limit=None, offset=0):
        return f"read:{path}:{limit}:{offset}"

    @staticmethod
    def run_write(path, content):
        return f"write:{path}:{content}"

    @staticmethod
    def run_edit(path, old_text, new_text):
        return f"edit:{path}:{old_text}:{new_text}"

    @staticmethod
    def run_glob(pattern):
        return f"glob:{pattern}"

    def runtime(self, *, max_steps=30):
        return SubagentRuntime(
            SubagentRuntimeConfig(
                workspace=Path("/workspace"),
                max_steps=max_steps,
                max_tokens=8000,
            ),
            SubagentRuntimeDependencies(
                create_message=self.create_message,
                trigger_hooks=self.trigger_hooks,
                call_tool_handler=self.call_tool_handler,
                run_bash=self.run_bash,
                run_read=self.run_read,
                run_write=self.run_write,
                run_edit=self.run_edit,
                run_glob=self.run_glob,
            ),
        )


class SubagentRuntimeTests(unittest.TestCase):
    def test_plain_text_response_returns_summary(self):
        fx = Fixture()
        fx.responses = [FakeResponse([FakeTextBlock("done")])]

        result = fx.runtime().run("inspect the repository")

        self.assertEqual(result, "done")
        self.assertEqual(len(fx.model_calls), 1)
        self.assertEqual(fx.model_calls[0]["messages"][0]["content"],
                         "inspect the repository")

    def test_system_prompt_identifies_short_lived_coding_subagent(self):
        fx = Fixture()
        fx.responses = [FakeResponse([FakeTextBlock("done")])]

        fx.runtime().run("task")

        system = fx.model_calls[0]["system"]
        self.assertIn("coding subagent", system)
        self.assertIn(str(Path("/workspace")), system)
        self.assertIn("Do not spawn more agents", system)

    def test_runtime_exposes_only_restricted_subagent_tools(self):
        names = [tool["name"] for tool in SUBAGENT_TOOLS]
        self.assertEqual(
            names,
            ["bash", "read_file", "write_file", "edit_file", "glob"],
        )
        self.assertNotIn("task", names)
        self.assertNotIn("spawn_teammate", names)

    def test_tool_call_executes_handler_and_feeds_result_back(self):
        fx = Fixture()
        fx.responses = [
            FakeResponse([FakeToolBlock("read_file", {"path": "a.txt"})]),
            FakeResponse([FakeTextBlock("summary")]),
        ]

        result = fx.runtime().run("read a file")

        self.assertEqual(result, "summary")
        self.assertEqual(len(fx.model_calls), 2)
        followup = fx.model_calls[1]["messages"][-1]
        self.assertEqual(followup["role"], "user")
        self.assertEqual(followup["content"][0]["type"], "tool_result")
        self.assertIn("read:a.txt", followup["content"][0]["content"])
        events = [event for event, _ in fx.hook_calls]
        self.assertIn("PreToolUse", events)
        self.assertIn("PostToolUse", events)

    def test_pre_tool_hook_can_block_execution(self):
        fx = Fixture()
        fx.blocked = "Permission denied"
        fx.responses = [
            FakeResponse([FakeToolBlock("bash", {"command": "danger"})]),
            FakeResponse([FakeTextBlock("stopped")]),
        ]

        result = fx.runtime().run("run command")

        self.assertEqual(result, "stopped")
        self.assertEqual(fx.handler_calls, [])
        followup = fx.model_calls[1]["messages"][-1]
        self.assertEqual(followup["content"][0]["content"],
                         "Permission denied")
        events = [event for event, _ in fx.hook_calls]
        self.assertNotIn("PostToolUse", events)

    def test_multiple_tool_calls_in_one_response_all_execute(self):
        fx = Fixture()
        fx.responses = [
            FakeResponse([
                FakeToolBlock("glob", {"pattern": "*.py"}, "tool_1"),
                FakeToolBlock("bash", {"command": "echo ok"}, "tool_2"),
            ]),
            FakeResponse([FakeTextBlock("done")]),
        ]

        fx.runtime().run("inspect")

        followup = fx.model_calls[1]["messages"][-1]["content"]
        self.assertEqual([item["tool_use_id"] for item in followup],
                         ["tool_1", "tool_2"])
        self.assertEqual(len(fx.handler_calls), 2)

    def test_max_steps_bounds_model_calls(self):
        fx = Fixture()
        fx.responses = [
            FakeResponse([FakeToolBlock("glob", {"pattern": "*.py"})]),
            FakeResponse([FakeToolBlock("glob", {"pattern": "*.txt"})]),
        ]

        result = fx.runtime(max_steps=2).run("loop")

        self.assertEqual(len(fx.model_calls), 2)
        self.assertEqual(result, "Subagent finished without a text summary.")

    def test_last_available_text_is_returned_after_tool_round(self):
        fx = Fixture()
        fx.responses = [
            FakeResponse([
                FakeTextBlock("intermediate note"),
                FakeToolBlock("glob", {"pattern": "*.py"}),
            ]),
        ]

        result = fx.runtime(max_steps=1).run("inspect")

        self.assertEqual(result, "intermediate note")

    def test_model_call_uses_configured_token_budget(self):
        fx = Fixture()
        fx.responses = [FakeResponse([FakeTextBlock("done")])]

        fx.runtime().run("task")

        self.assertEqual(fx.model_calls[0]["max_tokens"], 8000)


if __name__ == "__main__":
    unittest.main()
