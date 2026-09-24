from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_runtime.tools import ToolExecutionConfig, ToolExecutionRuntime


class ToolExecutionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runtime = ToolExecutionRuntime(
            ToolExecutionConfig(workspace=self.workspace)
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_resolve_path_stays_inside_workspace(self):
        resolved = self.runtime.resolve_path("proof/main.tex")
        self.assertEqual(resolved, (self.workspace / "proof" / "main.tex").resolve())

    def test_resolve_path_rejects_workspace_escape(self):
        with self.assertRaises(ValueError):
            self.runtime.resolve_path("../outside.txt")

    def test_write_read_and_edit_file(self):
        self.assertEqual(
            self.runtime.write_file("notes/test.txt", "alpha\nbeta\ngamma"),
            "Wrote 16 bytes to notes/test.txt",
        )
        self.assertEqual(
            self.runtime.read_file("notes/test.txt", offset=1, limit=1),
            "beta\n... (1 more lines)",
        )
        self.assertEqual(
            self.runtime.edit_file("notes/test.txt", "beta", "BETA"),
            "Edited notes/test.txt",
        )
        self.assertEqual(
            self.runtime.read_file("notes/test.txt"),
            "alpha\nBETA\ngamma",
        )

    def test_worktree_cwd_scopes_filesystem_operations(self):
        worktree = self.workspace / "worktree-a"
        worktree.mkdir()
        self.runtime.write_file("proof.tex", "inside", cwd=worktree)
        self.assertEqual((worktree / "proof.tex").read_text(), "inside")
        self.assertFalse((self.workspace / "proof.tex").exists())
        self.assertIn("Path escapes workspace", self.runtime.read_file("../outside", cwd=worktree))

    def test_glob_is_scoped_to_selected_root(self):
        (self.workspace / "a.py").write_text("a")
        (self.workspace / "b.txt").write_text("b")
        nested = self.workspace / "nested"
        nested.mkdir()
        (nested / "c.py").write_text("c")
        self.assertEqual(self.runtime.glob("*.py"), "a.py")
        self.assertEqual(self.runtime.glob("*.py", cwd=nested), "c.py")

    def test_bash_executes_in_requested_directory(self):
        output = self.runtime.run_bash("echo tool-runtime")
        self.assertIn("tool-runtime", output)

    def test_bash_tool_no_longer_invokes_shell_operators(self):
        output = self.runtime.run_bash("echo first && echo second")
        self.assertIn("Sandbox rejected command", output)
        self.assertIn("shell operator", output)

    def test_bash_rejects_cwd_outside_workspace(self):
        with tempfile.TemporaryDirectory() as outside:
            output = self.runtime.run_bash("echo nope", cwd=Path(outside))
        self.assertIn("cwd escapes workspace", output)

    def test_call_handler_preserves_unknown_and_type_error_contract(self):
        self.assertEqual(
            self.runtime.call_handler(None, {}, "missing"),
            "Unknown: missing",
        )

        def handler(required: str) -> str:
            return required

        error = self.runtime.call_handler(handler, {}, "demo")
        self.assertTrue(error.startswith("Error:"))

    def test_output_is_truncated_by_configured_limit(self):
        runtime = ToolExecutionRuntime(
            ToolExecutionConfig(workspace=self.workspace, max_output_chars=4)
        )
        output = runtime.run_bash("echo 123456789")
        self.assertEqual(output, "1234")

    def test_resource_limits_are_forwarded_to_command_executor(self):
        runtime = ToolExecutionRuntime(
            ToolExecutionConfig(
                workspace=self.workspace,
                memory_limit_mb=512,
                cpu_time_seconds=9,
                max_processes=5,
            )
        )
        config = runtime.command_executor.process_sandbox.config
        self.assertEqual(config.memory_limit_mb, 512)
        self.assertEqual(config.cpu_time_seconds, 9)
        self.assertEqual(config.max_processes, 5)

    def test_strict_network_mode_is_exposed_by_tool_runtime(self):
        runtime = ToolExecutionRuntime(
            ToolExecutionConfig(
                workspace=self.workspace,
                require_network_isolation=True,
            )
        )
        script = self.workspace / "network_mode.py"
        script.write_text("print('should-not-run')\n", encoding="utf-8")
        import os, shlex, sys
        command = (
            f'"{sys.executable}" "{script}"'
            if os.name == "nt"
            else f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
        )
        output = runtime.run_bash(command)
        self.assertIn("network isolation is required", output)


if __name__ == "__main__":
    unittest.main()
