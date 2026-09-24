from __future__ import annotations

import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from agent_runtime.security import SandboxedCommandConfig, SandboxedCommandExecutor


def _quote(value: str) -> str:
    if os.name == "nt":
        return f'"{value}"'
    return shlex.quote(value)


class SandboxedCommandExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.executor = SandboxedCommandExecutor(
            SandboxedCommandConfig(
                workspace=self.workspace,
                timeout_seconds=2.0,
                max_output_chars=10_000,
            )
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _python_script(self, name: str, source: str) -> str:
        path = self.workspace / name
        path.write_text(source, encoding="utf-8")
        return f"{_quote(sys.executable)} {_quote(str(path))}"

    def test_internal_echo_remains_available_without_shell(self):
        self.assertEqual(self.executor.execute("echo hello sandbox"), "hello sandbox")

    def test_python_workspace_script_executes(self):
        command = self._python_script("ok.py", "print('sandbox-ok')\n")
        self.assertEqual(self.executor.execute(command), "sandbox-ok")

    def test_shell_chaining_and_redirection_are_rejected(self):
        chained = self.executor.execute("echo first && echo second")
        redirected = self.executor.execute("echo first > out.txt")
        self.assertIn("Sandbox rejected command", chained)
        self.assertIn("shell operator", chained)
        self.assertIn("Sandbox rejected command", redirected)
        self.assertFalse((self.workspace / "out.txt").exists())

    def test_shell_interpreter_is_rejected(self):
        output = self.executor.execute("sh -c echo")
        self.assertIn("Sandbox rejected command", output)
        self.assertIn("blocked", output)

    def test_non_allowlisted_executable_is_rejected(self):
        output = self.executor.execute("definitely-not-an-allowed-program --version")
        self.assertIn("not allowlisted", output)

    def test_python_inline_code_is_rejected(self):
        command = f'{_quote(sys.executable)} -c "print(123)"'
        output = self.executor.execute(command)
        self.assertIn("Sandbox rejected command", output)
        self.assertIn("-c", output)

    def test_python_module_allowlist_accepts_unittest_and_rejects_http_server(self):
        allowed = self.executor.execute(f"{_quote(sys.executable)} -m unittest -h")
        rejected = self.executor.execute(
            f"{_quote(sys.executable)} -m http.server 8123"
        )
        self.assertNotIn("Sandbox rejected command", allowed)
        self.assertIn("Python module 'http.server' is not allowlisted", rejected)

    def test_python_script_outside_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / "outside.py"
            outside.write_text("print('outside')\n", encoding="utf-8")
            command = f"{_quote(sys.executable)} {_quote(str(outside))}"
            output = self.executor.execute(command)
        self.assertIn("Python script must remain inside workspace", output)

    def test_cwd_outside_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside_dir:
            output = self.executor.execute("echo nope", cwd=Path(outside_dir))
        self.assertIn("cwd escapes workspace", output)

    def test_parent_api_key_is_not_inherited(self):
        old = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "should-not-leak"
        try:
            command = self._python_script(
                "env_check.py",
                "import os\nprint(os.getenv('ANTHROPIC_API_KEY', '<missing>'))\n",
            )
            output = self.executor.execute(command)
        finally:
            if old is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = old
        self.assertEqual(output, "<missing>")

    def test_timeout_terminates_long_running_command(self):
        executor = SandboxedCommandExecutor(
            SandboxedCommandConfig(
                workspace=self.workspace,
                timeout_seconds=0.2,
                max_output_chars=10_000,
            )
        )
        command = self._python_script(
            "sleep.py",
            "import time\ntime.sleep(5)\nprint('late')\n",
        )
        output = executor.execute(command)
        self.assertIn("Timeout (0.2s)", output)

    def test_output_is_truncated(self):
        executor = SandboxedCommandExecutor(
            SandboxedCommandConfig(
                workspace=self.workspace,
                timeout_seconds=2,
                max_output_chars=4,
            )
        )
        self.assertEqual(executor.execute("echo 123456789"), "1234")

    def test_oversized_command_is_rejected_before_launch(self):
        executor = SandboxedCommandExecutor(
            SandboxedCommandConfig(
                workspace=self.workspace,
                max_command_chars=8,
            )
        )
        output = executor.execute("echo 123456789")
        self.assertIn("command exceeds 8 characters", output)

    def test_subprocess_output_is_bounded_while_drained(self):
        executor = SandboxedCommandExecutor(
            SandboxedCommandConfig(
                workspace=self.workspace,
                timeout_seconds=2,
                max_output_chars=64,
            )
        )
        command = self._python_script(
            "lots.py",
            "print('x' * 200000)\n",
        )
        output = executor.execute(command)
        self.assertEqual(output, "x" * 64)

    def test_home_and_temp_are_rebased_inside_workspace(self):
        command = self._python_script(
            "env_roots.py",
            "import os\n"
            "print(os.environ['HOME'])\n"
            "print(os.environ['TEMP'])\n",
        )
        output = self.executor.execute(command)
        lines = output.splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(Path(line).resolve().is_relative_to(self.workspace))

    @unittest.skipIf(os.name == "nt", "POSIX executable bit test")
    def test_explicit_workspace_executable_is_not_trusted(self):
        fake = self.workspace / "git"
        fake.write_text("#!/bin/sh\necho hijacked\n", encoding="utf-8")
        fake.chmod(0o755)
        output = self.executor.execute("./git --version")
        self.assertIn("not a trusted installed executable", output)
        self.assertNotIn("hijacked", output)

    def test_process_sandbox_resource_config_is_wired(self):
        executor = SandboxedCommandExecutor(
            SandboxedCommandConfig(
                workspace=self.workspace,
                memory_limit_mb=384,
                cpu_time_seconds=7,
                max_processes=3,
            )
        )
        self.assertEqual(executor.process_sandbox.config.memory_limit_mb, 384)
        self.assertEqual(executor.process_sandbox.config.cpu_time_seconds, 7)
        self.assertEqual(executor.process_sandbox.config.max_processes, 3)

    def test_strict_network_requirement_fails_closed_before_launch(self):
        executor = SandboxedCommandExecutor(
            SandboxedCommandConfig(
                workspace=self.workspace,
                require_network_isolation=True,
            )
        )
        command = self._python_script("never_runs.py", "print('should-not-run')\n")
        output = executor.execute(command)
        self.assertIn("Sandbox rejected command", output)
        self.assertIn("network isolation is required", output)
        self.assertNotIn("should-not-run", output)

    def test_strict_filesystem_requirement_fails_closed_before_launch(self):
        executor = SandboxedCommandExecutor(
            SandboxedCommandConfig(
                workspace=self.workspace,
                require_filesystem_isolation=True,
            )
        )
        command = self._python_script("never_runs_fs.py", "print('should-not-run')\n")
        output = executor.execute(command)
        self.assertIn("Sandbox rejected command", output)
        self.assertIn("filesystem isolation is required", output)
        self.assertNotIn("should-not-run", output)


if __name__ == "__main__":
    unittest.main()
