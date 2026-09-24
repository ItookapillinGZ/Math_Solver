from __future__ import annotations

import unittest
from types import SimpleNamespace

from agent_runtime.cli import (
    AgentCLIApplication,
    AgentCLIApplicationDependencies,
    TerminalEventRenderer,
)


class FakeCronScheduler:
    def __init__(self) -> None:
        self.calls = []

    def autorun_loop(self, history, context, **kwargs):
        self.calls.append((history, context, kwargs))
        return None


class AgentCLIApplicationTests(unittest.TestCase):
    def make_app(self, inputs):
        outputs: list[str] = []
        iterator = iter(inputs)
        renderer = TerminalEventRenderer(
            "s20 >> ",
            input_fn=lambda _prompt: next(iterator),
            output_fn=outputs.append,
        )
        hooks = []
        seen_histories = []
        cron = FakeCronScheduler()

        def update_context(_context, messages):
            return {"message_count": len(messages)}

        def agent_loop(messages, _context):
            seen_histories.append(list(messages))
            messages.append({
                "role": "assistant",
                "content": [SimpleNamespace(type="text", text="assistant reply")],
            })

        app = AgentCLIApplication(
            AgentCLIApplicationDependencies(
                renderer=renderer,
                terminal_print=renderer.emit,
                trigger_hooks=lambda event, payload: hooks.append((event, payload)),
                update_context=update_context,
                agent_loop=agent_loop,
                consume_lead_inbox=lambda route_protocol=True: [],
                cron_scheduler=cron,
            )
        )
        return app, renderer, outputs, hooks, seen_histories, cron

    def test_run_processes_one_turn_and_prints_assistant_text(self):
        app, renderer, outputs, hooks, histories, _cron = self.make_app(
            ["hello", "q"]
        )
        app.run()
        self.assertIn("assistant reply", outputs)
        self.assertEqual(hooks, [("UserPromptSubmit", "hello")])
        self.assertEqual(histories[0][0]["content"], "hello")
        self.assertFalse(renderer.interactive)

    def test_empty_input_exits_without_agent_turn(self):
        app, renderer, _outputs, hooks, histories, _cron = self.make_app([""])
        app.run()
        self.assertEqual(hooks, [])
        self.assertEqual(histories, [])
        self.assertFalse(renderer.interactive)

    def test_inbox_is_appended_after_turn(self):
        outputs = []
        iterator = iter(["hello", "q"])
        renderer = TerminalEventRenderer(
            "s20 >> ", input_fn=lambda _p: next(iterator), output_fn=outputs.append
        )
        captured = []
        inbox_reads = {"count": 0}

        def consume(route_protocol=True):
            inbox_reads["count"] += 1
            if inbox_reads["count"] == 1:
                return [{
                    "from": "proof_worker",
                    "type": "message",
                    "content": "status ready",
                    "metadata": {},
                }]
            return []

        def loop(messages, _context):
            captured.append(messages)

        app = AgentCLIApplication(AgentCLIApplicationDependencies(
            renderer=renderer,
            terminal_print=renderer.emit,
            trigger_hooks=lambda *_args: None,
            update_context=lambda _ctx, msgs: {"n": len(msgs)},
            agent_loop=loop,
            consume_lead_inbox=consume,
            cron_scheduler=FakeCronScheduler(),
        ))
        app.run()
        history = captured[0]
        self.assertTrue(any(
            msg.get("role") == "user" and "[Inbox]" in msg.get("content", "")
            for msg in history
        ))

    def test_print_turn_assistants_ignores_non_text_and_old_turns(self):
        app, _renderer, outputs, _hooks, _histories, _cron = self.make_app(["q"])
        messages = [
            {"role": "assistant", "content": [SimpleNamespace(type="text", text="old")]},
            {"role": "assistant", "content": [
                SimpleNamespace(type="tool_use", text="ignored"),
                SimpleNamespace(type="text", text="new"),
                {"type": "text", "text": "workflow failure"},
            ]},
        ]
        app.print_turn_assistants(messages, 1)
        self.assertEqual(outputs, ["new", "workflow failure"])

    def test_cron_autorun_receives_same_agent_lock_and_callbacks(self):
        app, _renderer, _outputs, _hooks, _histories, cron = self.make_app(["q"])
        history = []
        context = {}
        app.cron_autorun_loop(history, context)
        self.assertEqual(len(cron.calls), 1)
        _history, _context, kwargs = cron.calls[0]
        self.assertIs(kwargs["agent_lock"], app.agent_lock)
        self.assertIs(kwargs["agent_loop"], app.deps.agent_loop)
        self.assertIs(kwargs["update_context"], app.deps.update_context)
        self.assertTrue(callable(kwargs["print_turn_assistants"]))


if __name__ == "__main__":
    unittest.main()
