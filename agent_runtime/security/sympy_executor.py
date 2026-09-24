from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .process_sandbox import ProcessSandbox, ProcessSandboxConfig


@dataclass(frozen=True)
class SafeSympyConfig:
    """Limits for restricted symbolic verification.

    The verifier intentionally keeps arbitrary model-generated Python outside
    the Agent process.  POSIX resource limits are enforced by the worker itself;
    Windows currently relies on the child-process boundary, static language
    restrictions, temporary cwd, minimal environment, and wall-clock timeout.
    Strong OS-level Windows memory/network isolation is a later sandbox layer.
    """

    timeout_seconds: float = 5.0
    max_code_chars: int = 64_000
    max_output_chars: int = 16_000
    memory_limit_mb: int = 768
    cpu_limit_seconds: int = 5
    max_processes: int = 1

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_code_chars < 1:
            raise ValueError("max_code_chars must be positive")
        if self.max_output_chars < 1:
            raise ValueError("max_output_chars must be positive")
        if self.memory_limit_mb < 64:
            raise ValueError("memory_limit_mb must be at least 64")
        if self.cpu_limit_seconds < 1:
            raise ValueError("cpu_limit_seconds must be positive")
        if self.max_processes < 1:
            raise ValueError("max_processes must be positive")


class SafeSympyExecutor:
    """Execute restricted SymPy snippets in a short-lived isolated process.

    Security properties of this layer:
    - no model-generated code executes in the Agent process;
    - the worker starts with ``python -E -s -P -B -X utf8`` and a temporary working directory;
    - imports, dangerous builtins, dunder/introspection access, and common
      file/network/process escape surfaces are rejected by the worker AST gate;
    - stdin/code size, captured output, and wall-clock time are bounded;
    - the parent additionally attaches OS-level process limits: Windows Job
      Objects enforce memory/CPU/process-count limits; POSIX uses ``prlimit``
      when available. The worker also retains its own POSIX rlimit defense.

    This is deliberately not described as a complete OS sandbox. Job Objects
    provide hard Windows resource/process-tree limits, but neither built-in
    backend provides a true network namespace or filesystem jail.
    """

    def __init__(self, config: SafeSympyConfig | None = None) -> None:
        self.config = config or SafeSympyConfig()
        self._worker_path = Path(__file__).with_name("_sympy_worker.py").resolve()
        self.process_sandbox = ProcessSandbox(
            ProcessSandboxConfig(
                memory_limit_mb=self.config.memory_limit_mb,
                cpu_time_seconds=self.config.cpu_limit_seconds,
                max_processes=self.config.max_processes,
            )
        )

    def execute(self, sympy_code: str) -> str:
        code = str(sympy_code)
        if len(code) > self.config.max_code_chars:
            return (
                "Symbolic Execution Rejected by sandbox.\n"
                f"[Reason]: code exceeds {self.config.max_code_chars} characters."
            )
        if not code.strip():
            return (
                "Symbolic Execution Rejected by sandbox.\n"
                "[Reason]: empty SymPy program."
            )

        request = {
            "code": code,
            "max_output_chars": self.config.max_output_chars,
            "memory_limit_mb": self.config.memory_limit_mb,
            "cpu_limit_seconds": self.config.cpu_limit_seconds,
        }
        payload = json.dumps(request, ensure_ascii=True)

        with tempfile.TemporaryDirectory(prefix="researchagent_sympy_") as temp_dir:
            env = self._minimal_environment(temp_dir)
            try:
                proc, sandbox_handle = self._start_worker(temp_dir, env)
            except Exception as exc:
                return (
                    "Symbolic Execution Failed in sandbox worker.\n"
                    f"[Error Message]: sandbox setup failed: {exc}"
                )
            try:
                try:
                    stdout, stderr = proc.communicate(
                        payload,
                        timeout=self.config.timeout_seconds,
                    )
                except subprocess.TimeoutExpired:
                    sandbox_handle.terminate()
                    try:
                        proc.communicate(timeout=1.0)
                    except Exception:
                        pass
                    return (
                        "Symbolic Execution Timed Out.\n"
                        f"[Timeout]: {self.config.timeout_seconds:g} seconds"
                    )
            finally:
                sandbox_handle.close()

        if proc.returncode != 0:
            detail = self._clip(stderr.strip() or stdout.strip() or "worker failed")
            return (
                "Symbolic Execution Failed in sandbox worker.\n"
                f"[Error Message]: {detail}"
            )

        try:
            response = json.loads(stdout)
        except json.JSONDecodeError:
            return (
                "Symbolic Execution Failed in sandbox worker.\n"
                "[Error Message]: worker returned invalid structured output."
            )
        return self._format_response(response)

    def _worker_command(self) -> list[str]:
        # ``-I`` can hide a virtual environment's site-packages on some
        # Windows/Python combinations, which prevents the child from importing
        # the project's SymPy installation.  Use the protections we actually
        # need explicitly instead:
        #   -E  ignore PYTHON* environment variables
        #   -s  do not add the user site-packages directory
        #   -P  do not prepend cwd/script directory to sys.path
        #   -B  do not write bytecode
        #   -X utf8  make pipe diagnostics deterministic on Windows
        # Regular venv site-packages remain available, so the worker imports
        # the same installed SymPy package as the parent environment.
        return [
            sys.executable,
            "-E",
            "-s",
            "-P",
            "-B",
            "-X",
            "utf8",
            str(self._worker_path),
        ]

    def _start_worker(
        self,
        cwd: str,
        env: dict[str, str],
    ) -> tuple[subprocess.Popen[str], Any]:
        command = self._worker_command()
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "env": env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            creationflags = 0
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(command, **kwargs)
        try:
            sandbox_handle = self.process_sandbox.attach(proc)
        except Exception:
            self._terminate_worker(proc)
            raise
        return proc, sandbox_handle

    @staticmethod
    def _minimal_environment(temp_dir: str) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in ("SYSTEMROOT", "WINDIR"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        # Keep all writable process-local paths inside the temporary directory.
        env["HOME"] = temp_dir
        env["TMP"] = temp_dir
        env["TEMP"] = temp_dir
        # Locale variables are not security-sensitive and avoid platform-specific
        # encoding surprises in native/third-party dependencies.
        for key in ("LANG", "LC_ALL"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    @staticmethod
    def _terminate_worker(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        if os.name != "nt":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError):
                pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    def _format_response(self, response: Any) -> str:
        if not isinstance(response, dict):
            return (
                "Symbolic Execution Failed in sandbox worker.\n"
                "[Error Message]: malformed worker response."
            )

        status = response.get("status")
        if status == "rejected":
            return (
                "Symbolic Execution Rejected by sandbox.\n"
                f"[Reason]: {self._clip(str(response.get('message', 'policy violation')))}"
            )
        if status == "error":
            error_type = str(response.get("error_type", "SymPyError"))
            message = self._clip(str(response.get("message", "unknown error")))
            return (
                "Symbolic Execution Failed due to Python/SymPy error.\n"
                f"[Error Type]: {error_type}\n"
                f"[Error Message]: {message}\n"
                "[Tip]: Use only the restricted SymPy API; define symbols with "
                "symbols() and assign the final expression to 'result'."
            )
        if status != "ok":
            return (
                "Symbolic Execution Failed in sandbox worker.\n"
                "[Error Message]: unknown worker status."
            )

        result = response.get("result")
        latex = response.get("latex")
        printed = response.get("stdout")
        truncated = bool(response.get("truncated"))

        if result is not None:
            text = f"Symbolic Execution Success.\n[Result]: {self._clip(str(result))}"
            if latex:
                text += f"\n[LaTeX Form]: {self._clip(str(latex))}"
        elif printed:
            text = (
                "Symbolic Execution Success (via print).\n"
                f"[Output]: {self._clip(str(printed))}"
            )
        else:
            text = (
                "Symbolic Execution Success, but no 'result' variable or print "
                "output was found. Please assign your target expression to the "
                "'result' variable."
            )
        if truncated:
            text += "\n[Sandbox]: output was truncated to the configured limit."
        return text

    def _clip(self, text: str) -> str:
        limit = self.config.max_output_chars
        if len(text) <= limit:
            return text
        return text[:limit] + "...<truncated>"
