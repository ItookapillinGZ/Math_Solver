from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from agent_runtime.security import SafeSympyConfig, SafeSympyExecutor


class SafeSympyExecutorTests(unittest.TestCase):
    def executor(self, **overrides) -> SafeSympyExecutor:
        config = SafeSympyConfig(
            timeout_seconds=overrides.get("timeout_seconds", 4.0),
            max_code_chars=overrides.get("max_code_chars", 8_000),
            max_output_chars=overrides.get("max_output_chars", 2_000),
            memory_limit_mb=overrides.get("memory_limit_mb", 768),
            cpu_limit_seconds=overrides.get("cpu_limit_seconds", 2),
        )
        return SafeSympyExecutor(config)


    def test_worker_uses_venv_compatible_isolation_flags(self):
        command = self.executor()._worker_command()
        self.assertEqual(command[0], __import__("sys").executable)
        self.assertNotIn("-I", command)
        for flag in ("-E", "-s", "-P", "-B"):
            self.assertIn(flag, command)
        self.assertIn("-X", command)
        self.assertIn("utf8", command)

    def test_symbolic_result_and_latex(self):
        output = self.executor().execute(
            "x = symbols('x')\nresult = expand((x + 1)**2)"
        )
        self.assertIn("Symbolic Execution Success", output)
        self.assertIn("x**2 + 2*x + 1", output)
        self.assertIn("LaTeX Form", output)

    def test_sp_proxy_supports_common_model_generated_code(self):
        output = self.executor().execute(
            "x = sp.symbols('x')\nresult = sp.diff(sp.sin(x), x)"
        )
        self.assertIn("cos(x)", output)

    def test_print_only_program_is_supported(self):
        output = self.executor().execute("print(factor(Integer(12)))")
        self.assertIn("Success (via print)", output)
        self.assertIn("12", output)

    def test_import_is_rejected(self):
        output = self.executor().execute("import os\nresult = 1")
        self.assertIn("Rejected by sandbox", output)
        self.assertIn("Import", output)

    def test_file_access_builtin_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "owned.txt"
            code = f"open({str(marker)!r}, 'w').write('owned')\nresult = 1"
            output = self.executor().execute(code)
            self.assertIn("Rejected by sandbox", output)
            self.assertFalse(marker.exists())

    def test_dunder_introspection_is_rejected(self):
        output = self.executor().execute("result = (1).__class__")
        self.assertIn("Rejected by sandbox", output)
        self.assertIn("__class__", output)

    def test_dynamic_import_builtin_is_rejected(self):
        output = self.executor().execute("result = __import__('os')")
        self.assertIn("Rejected by sandbox", output)
        self.assertIn("__import__", output)

    def test_infinite_loop_is_killed_by_wall_clock_timeout(self):
        executor = self.executor(timeout_seconds=0.25, cpu_limit_seconds=1)
        started = time.monotonic()
        output = executor.execute("while True:\n    pass")
        elapsed = time.monotonic() - started
        self.assertIn("Timed Out", output)
        self.assertLess(elapsed, 2.0)

    def test_code_size_limit_rejects_before_spawning(self):
        output = self.executor(max_code_chars=10).execute("result = " + "1" * 20)
        self.assertIn("Rejected by sandbox", output)
        self.assertIn("exceeds", output)

    def test_output_is_bounded(self):
        output = self.executor(max_output_chars=64).execute("print('x' * 10000)")
        self.assertIn("output was truncated", output)
        self.assertLess(len(output), 500)

    def test_python_syntax_error_is_returned_as_rejection(self):
        output = self.executor().execute("result = (")
        self.assertIn("Rejected by sandbox", output)


if __name__ == "__main__":
    unittest.main()
