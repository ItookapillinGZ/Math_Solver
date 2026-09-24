from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .process_sandbox import ProcessSandbox, ProcessSandboxConfig


_DEFAULT_ALLOWED_EXECUTABLES = (
    "python",
    "git",
    "node",
    "ruff",
    "mypy",
    "black",
    "uv",
    "cargo",
    "rustc",
    "go",
    "java",
    "javac",
    "cmake",
    "make",
    "ninja",
    "latexmk",
    "pdflatex",
    "xelatex",
    "lualatex",
    "bibtex",
)

_DEFAULT_ALLOWED_PYTHON_MODULES = (
    "unittest",
    "pytest",
    "compileall",
)

# Only process-launch essentials are inherited. HOME/TEMP are replaced with a
# per-command directory inside the workspace and secrets/API keys are omitted.
_DEFAULT_INHERITED_ENV = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "LANG",
    "LC_ALL",
)

_BLOCKED_EXECUTABLES = frozenset(
    {
        "cmd",
        "command",
        "powershell",
        "pwsh",
        "bash",
        "sh",
        "zsh",
        "fish",
        "wsl",
        "busybox",
        "cscript",
        "wscript",
    }
)

_BLOCKED_EXECUTABLE_SUFFIXES = frozenset({".bat", ".cmd", ".ps1", ".vbs"})


@dataclass(frozen=True)
class SandboxedCommandConfig:
    """Configuration for direct, no-shell command execution.

    This layer removes the host-shell boundary (``shell=True``), narrows model
    commands to a fixed executable allowlist, and applies OS-level CPU/memory
    resource controls through :class:`ProcessSandbox`. The built-in backends do
    not provide a filesystem jail or network namespace; callers can request
    those capabilities and fail closed when unavailable.
    """

    workspace: Path
    timeout_seconds: float = 120.0
    max_command_chars: int = 16_000
    max_output_chars: int = 50_000
    text_encoding: str = "utf-8"
    allowed_executables: tuple[str, ...] = field(
        default_factory=lambda: _DEFAULT_ALLOWED_EXECUTABLES
    )
    allowed_python_modules: tuple[str, ...] = field(
        default_factory=lambda: _DEFAULT_ALLOWED_PYTHON_MODULES
    )
    inherited_environment: tuple[str, ...] = field(
        default_factory=lambda: _DEFAULT_INHERITED_ENV
    )
    memory_limit_mb: int = 1024
    cpu_time_seconds: int = 30
    max_processes: int = 16
    require_network_isolation: bool = False
    require_filesystem_isolation: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_command_chars < 1:
            raise ValueError("max_command_chars must be positive")
        if self.max_output_chars < 1:
            raise ValueError("max_output_chars must be positive")
        if self.memory_limit_mb < 64:
            raise ValueError("memory_limit_mb must be at least 64")
        if self.cpu_time_seconds < 1:
            raise ValueError("cpu_time_seconds must be positive")
        if self.max_processes < 1:
            raise ValueError("max_processes must be positive")


class SandboxedCommandExecutor:
    """Run one allowlisted process without invoking a host command shell.

    Security properties of this layer:
    - command strings are parsed into argv and executed with ``shell=False``;
    - shell chaining/redirection/substitution syntax is rejected;
    - shell interpreters and batch/PowerShell wrappers are rejected;
    - the executable basename must be allowlisted and resolve to a trusted
      executable outside the writable workspace (the active Python interpreter
      is explicitly trusted even when the virtualenv lives inside workspace);
    - cwd must remain under the configured workspace;
    - Python ``-c`` / stdin / interactive execution is rejected; Python scripts
      must live inside the workspace, while ``-m`` is limited to safe test/build
      modules;
    - child environment omits API keys/secrets, sanitizes PATH, and re-homes
      HOME/TEMP to a per-command directory inside workspace;
    - stdin is closed, captured output is bounded while the process is running,
      wall time is bounded, and timeout cleanup terminates the process group/tree.

    Resource containment is additionally delegated to :class:`ProcessSandbox`:
    Windows uses a Job Object and POSIX uses ``prlimit`` when available. Hard
    network/filesystem isolation is represented explicitly as a capability; the
    built-in backends fail closed when strict isolation is required but cannot
    provide it.
    """

    def __init__(self, config: SandboxedCommandConfig) -> None:
        self.config = config
        self.workspace = Path(config.workspace).resolve()
        self._allowed = frozenset(
            self._normalize_name(name) for name in config.allowed_executables
        )
        self._allowed_python_modules = frozenset(config.allowed_python_modules)
        self._sandbox_root = self.workspace / ".state" / "command_sandbox"
        self._sandbox_root.mkdir(parents=True, exist_ok=True)
        self.process_sandbox = ProcessSandbox(
            ProcessSandboxConfig(
                memory_limit_mb=config.memory_limit_mb,
                cpu_time_seconds=config.cpu_time_seconds,
                max_processes=config.max_processes,
                require_network_isolation=config.require_network_isolation,
                require_filesystem_isolation=config.require_filesystem_isolation,
            )
        )

    def execute(self, command: str, cwd: Path | None = None) -> str:
        raw = str(command)
        if not raw.strip():
            return "Error: Sandbox rejected command: empty command"
        if len(raw) > self.config.max_command_chars:
            return (
                "Error: Sandbox rejected command: command exceeds "
                f"{self.config.max_command_chars} characters"
            )

        try:
            root = self._resolve_cwd(cwd)
            self._reject_shell_syntax(raw)
            argv = self._split_command(raw)
            if not argv:
                return "Error: Sandbox rejected command: empty command"

            # Keep the old teaching/demo contract without invoking a shell
            # builtin. Echo is implemented internally and cannot spawn a process.
            if self._normalize_name(argv[0]) == "echo" and not self._looks_like_path(argv[0]):
                return self._truncate(" ".join(argv[1:])) or "(no output)"

            with tempfile.TemporaryDirectory(
                prefix="cmd_",
                dir=self._sandbox_root,
            ) as command_home:
                env = self._build_environment(Path(command_home))
                executable = self._resolve_executable(argv[0], root, env)
                argv[0] = str(executable)
                self._validate_invocation(executable, argv[1:], root)
                return self._run_process(argv, root, env)
        except (ValueError, FileNotFoundError) as exc:
            return f"Error: Sandbox rejected command: {exc}"

    def _run_process(
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
    ) -> str:
        popen_kwargs: dict[str, object] = {
            "cwd": cwd,
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": False,
            "shell": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            ) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kwargs["start_new_session"] = True

        try:
            self.process_sandbox.validate_requirements()
        except RuntimeError as exc:
            return f"Error: Sandbox rejected command: {exc}"

        try:
            process = subprocess.Popen(argv, **popen_kwargs)
        except OSError as exc:
            return f"Error: command launch failed: {exc}"

        sandbox_handle = None
        try:
            sandbox_handle = self.process_sandbox.attach(process)
        except Exception as exc:
            self._terminate_process_tree(process)
            try:
                process.wait(timeout=2.0)
            except Exception:
                pass
            return f"Error: Sandbox setup failed: {exc}"

        output = bytearray()
        # UTF-8 can need four bytes per character. This bounds retained process
        # output without stopping the reader from draining the pipe.
        byte_limit = max(4, self.config.max_output_chars * 4)

        def drain() -> None:
            pipe = process.stdout
            if pipe is None:
                return
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    break
                remaining = byte_limit - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])

        reader = threading.Thread(target=drain, name="sandbox-output", daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=self.config.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            if sandbox_handle is not None:
                sandbox_handle.terminate()
            else:
                self._terminate_process_tree(process)
            try:
                process.wait(timeout=2.0)
            except Exception:
                pass
        reader.join(timeout=2.0)
        if process.stdout is not None:
            try:
                process.stdout.close()
            except Exception:
                pass
        if sandbox_handle is not None:
            sandbox_handle.close()

        if timed_out:
            return f"Error: Timeout ({self._format_timeout()}s)"

        text = bytes(output).decode(self.config.text_encoding, errors="replace").strip()
        if not text:
            if process.returncode:
                return f"Error: command exited with code {process.returncode}"
            return "(no output)"
        return self._truncate(text)

    def _resolve_cwd(self, cwd: Path | None) -> Path:
        root = Path(cwd).resolve() if cwd is not None else self.workspace
        if not root.is_relative_to(self.workspace):
            raise ValueError(f"cwd escapes workspace: {root}")
        if not root.exists() or not root.is_dir():
            raise ValueError(f"cwd does not exist: {root}")
        return root

    @staticmethod
    def _normalize_name(value: str) -> str:
        name = Path(str(value).strip('"\'')).name.lower()
        if name.endswith(".exe"):
            name = name[:-4]
        # Treat versioned Python executable names as the same trusted runtime.
        if re.fullmatch(r"python(?:3(?:\.\d+)?)?", name):
            return "python"
        return name

    @staticmethod
    def _looks_like_path(value: str) -> bool:
        text = str(value)
        return Path(text.strip('"\'')).is_absolute() or "/" in text or "\\" in text

    def _split_command(self, command: str) -> list[str]:
        try:
            parts = shlex.split(command, posix=(os.name != "nt"))
        except ValueError as exc:
            raise ValueError(f"cannot parse command: {exc}") from exc
        if os.name == "nt":
            parts = [self._strip_matching_quotes(part) for part in parts]
        return parts

    @staticmethod
    def _strip_matching_quotes(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value

    def _reject_shell_syntax(self, command: str) -> None:
        quote: str | None = None
        escaped = False
        i = 0
        while i < len(command):
            ch = command[i]
            if escaped:
                escaped = False
                i += 1
                continue
            if ch == "\\" and os.name != "nt":
                escaped = True
                i += 1
                continue
            if quote is not None:
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch in {'"', "'"}:
                quote = ch
                i += 1
                continue
            if ch in "\r\n|&;><`":
                raise ValueError(
                    f"shell operator {ch!r} is not allowed; run one process at a time"
                )
            if command.startswith("$(", i):
                raise ValueError("shell command substitution is not allowed")
            i += 1
        if quote is not None:
            raise ValueError("unterminated quote")

    def _build_environment(self, command_home: Path) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in self.config.inherited_environment:
            if key == "PATH":
                continue
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        env["PATH"] = self._sanitized_path()

        home = command_home / "home"
        temp = command_home / "tmp"
        home.mkdir(parents=True, exist_ok=True)
        temp.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["TMP"] = str(temp)
        env["TEMP"] = str(temp)
        env["TMPDIR"] = str(temp)
        env["PYTHONIOENCODING"] = self.config.text_encoding
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        # Do not inherit global/system git aliases/hooks/configuration. Repository
        # local config still applies because execution cwd stays in workspace.
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = "NUL" if os.name == "nt" else "/dev/null"
        return env

    def _sanitized_path(self) -> str:
        kept: list[str] = []
        seen: set[str] = set()
        for raw in os.environ.get("PATH", "").split(os.pathsep):
            item = raw.strip().strip('"')
            if not item or item in {".", ".."}:
                continue
            try:
                candidate = Path(item).resolve()
            except OSError:
                continue
            if candidate.is_relative_to(self.workspace):
                continue
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            kept.append(str(candidate))
        return os.pathsep.join(kept)

    def _resolve_executable(
        self, token: str, cwd: Path, env: dict[str, str]
    ) -> Path:
        raw = self._strip_matching_quotes(token)
        normalized = self._normalize_name(raw)
        if normalized in _BLOCKED_EXECUTABLES:
            raise ValueError(f"shell/interpreter executable '{normalized}' is blocked")
        if normalized not in self._allowed:
            raise ValueError(f"executable '{normalized}' is not allowlisted")

        active_python = Path(sys.executable).resolve()
        if normalized == "python" and not self._looks_like_path(raw):
            return active_python

        if self._looks_like_path(raw):
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            candidate = candidate.resolve()
            if not candidate.exists() or not candidate.is_file():
                raise FileNotFoundError(f"executable not found: {raw}")
            self._reject_wrapper_suffix(candidate)
            if candidate == active_python and normalized == "python":
                return candidate
            trusted = self._trusted_external_executables(normalized, env)
            if candidate not in trusted:
                raise ValueError(
                    "explicit executable path is not a trusted installed executable; "
                    "workspace executables are not launched directly"
                )
            return candidate

        resolved = shutil.which(raw, path=env.get("PATH"))
        if not resolved:
            raise FileNotFoundError(f"executable not found on sanitized PATH: {raw}")
        candidate = Path(resolved).resolve()
        self._reject_wrapper_suffix(candidate)
        if candidate.is_relative_to(self.workspace):
            raise ValueError("resolved executable is inside writable workspace")
        if self._normalize_name(candidate.name) != normalized:
            raise ValueError("resolved executable name did not match requested command")
        return candidate

    @staticmethod
    def _reject_wrapper_suffix(path: Path) -> None:
        if path.suffix.lower() in _BLOCKED_EXECUTABLE_SUFFIXES:
            raise ValueError(
                f"script wrapper '{path.suffix.lower()}' is not allowed as an executable"
            )

    def _trusted_external_executables(
        self,
        normalized: str,
        env: dict[str, str],
    ) -> set[Path]:
        trusted: set[Path] = set()
        active_python = Path(sys.executable).resolve()
        if normalized == "python":
            trusted.add(active_python)
        for spelling in (normalized, normalized + ".exe"):
            resolved = shutil.which(spelling, path=env.get("PATH"))
            if resolved:
                path = Path(resolved).resolve()
                if not path.is_relative_to(self.workspace):
                    trusted.add(path)
        return trusted

    def _validate_invocation(self, executable: Path, args: list[str], cwd: Path) -> None:
        name = self._normalize_name(executable.name)
        if name == "python":
            self._validate_python_args(args, cwd)

    def _validate_python_args(self, args: list[str], cwd: Path) -> None:
        if not args:
            return
        first = args[0]
        if first in {"-V", "--version"}:
            return
        if first in {"-c", "-i", "-", "--interactive"}:
            raise ValueError(f"Python option '{first}' is not allowed")
        if first == "-m":
            if len(args) < 2:
                raise ValueError("Python -m requires a module name")
            module = args[1]
            if module not in self._allowed_python_modules:
                raise ValueError(f"Python module '{module}' is not allowlisted")
            return
        if first.startswith("-"):
            raise ValueError(f"Python option '{first}' is not allowlisted")

        script = Path(first)
        if not script.is_absolute():
            script = cwd / script
        script = script.resolve()
        if not script.is_relative_to(self.workspace):
            raise ValueError("Python script must remain inside workspace")
        if script.suffix.lower() != ".py":
            raise ValueError("Python direct execution requires a .py script")
        if not script.exists() or not script.is_file():
            raise ValueError(f"Python script does not exist: {first}")

    def _terminate_process_tree(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            taskkill = system_root / "System32" / "taskkill.exe"
            if taskkill.exists():
                try:
                    subprocess.run(
                        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                        timeout=3,
                        check=False,
                    )
                except Exception:
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                except Exception:
                    pass
            return

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _truncate(self, output: str) -> str:
        return str(output)[: self.config.max_output_chars]

    def _format_timeout(self) -> str:
        value = self.config.timeout_seconds
        return str(int(value)) if float(value).is_integer() else str(value)
