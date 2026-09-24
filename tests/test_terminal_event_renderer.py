import threading
import unittest

from agent_runtime.cli import TerminalEventRenderer


class TerminalEventRendererTests(unittest.TestCase):
    def make_renderer(self, *, input_fn=None):
        output = []
        renderer = TerminalEventRenderer(
            "s20 >> ",
            input_fn=input_fn,
            output_fn=output.append,
        )
        return renderer, output

    def test_inactive_background_event_prints_immediately(self):
        renderer, output = self.make_renderer()

        thread = threading.Thread(target=lambda: renderer.emit("background"))
        thread.start()
        thread.join()

        self.assertEqual(output, ["background"])
        self.assertEqual(renderer.pending_count, 0)

    def test_interactive_background_event_is_queued(self):
        renderer, output = self.make_renderer()
        renderer.set_interactive(True)

        thread = threading.Thread(target=lambda: renderer.emit("[claim] task"))
        thread.start()
        thread.join()

        self.assertEqual(output, [])
        self.assertEqual(renderer.pending_count, 1)

    def test_flush_pending_preserves_fifo_order(self):
        renderer, output = self.make_renderer()
        renderer.set_interactive(True)

        def emit_all():
            renderer.emit("one")
            renderer.emit("two")
            renderer.emit("three")

        thread = threading.Thread(target=emit_all)
        thread.start()
        thread.join()

        flushed = renderer.flush_pending()

        self.assertEqual(flushed, ["one", "two", "three"])
        self.assertEqual(output, ["one", "two", "three"])
        self.assertEqual(renderer.pending_count, 0)

    def test_background_emit_never_redraws_prompt(self):
        renderer, output = self.make_renderer()
        renderer.set_interactive(True)

        thread = threading.Thread(
            target=lambda: renderer.emit("[bus] worker -> lead")
        )
        thread.start()
        thread.join()

        self.assertNotIn("s20 >> ", "".join(output))
        renderer.flush_pending()
        self.assertEqual(output, ["[bus] worker -> lead"])
        self.assertNotIn("s20 >> ", "".join(output))

    def test_main_thread_event_is_immediate_while_interactive(self):
        renderer, output = self.make_renderer()
        renderer.set_interactive(True)

        renderer.emit("lead output")

        self.assertEqual(output, ["lead output"])
        self.assertEqual(renderer.pending_count, 0)

    def test_read_input_flushes_events_queued_during_input(self):
        output = []
        holder = {}

        def fake_input(prompt):
            self.assertEqual(prompt, "s20 >> ")
            thread = threading.Thread(
                target=lambda: holder["renderer"].emit("late background")
            )
            thread.start()
            thread.join()
            self.assertEqual(output, [])
            return "hello"

        renderer = TerminalEventRenderer(
            "s20 >> ",
            input_fn=fake_input,
            output_fn=output.append,
        )
        holder["renderer"] = renderer
        renderer.set_interactive(True)

        query = renderer.read_input()

        self.assertEqual(query, "hello")
        self.assertEqual(output, ["late background"])
        self.assertEqual(renderer.pending_count, 0)

    def test_concurrent_background_events_are_not_lost(self):
        renderer, output = self.make_renderer()
        renderer.set_interactive(True)
        expected = {f"event-{i}" for i in range(20)}

        threads = [
            threading.Thread(target=lambda i=i: renderer.emit(f"event-{i}"))
            for i in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(renderer.pending_count, 20)
        renderer.flush_pending()
        self.assertEqual(set(output), expected)
        self.assertEqual(len(output), 20)


if __name__ == "__main__":
    unittest.main()
