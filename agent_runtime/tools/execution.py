from __future__ import annotations

import glob as globlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_runtime.security import SandboxedCommandConfig, SandboxedCommandExecutor


@dataclass(frozen=True)
class ToolExecutionConfig:
    """Configuration for workspace-scoped tool execution."""

    workspace: Path
    shell_timeout_seconds: int = 120
    max_output_chars: int = 50_000
    text_encoding: str = "utf-8"
    memory_limit_mb: int = 1024
    cpu_time_seconds: int = 30
    max_processes: int = 16
    require_network_isolation: bool = False
    require_filesystem_isolation: bool = False


class ToolExecutionRuntime:
    """Execute basic filesystem tools and no-shell allowlisted commands.

    Tool schemas live in the registry. Filesystem helpers remain workspace
    scoped, while ``run_bash`` is retained as a legacy tool name but delegates
    to :class:`SandboxedCommandExecutor`; it no longer invokes a host shell.
    """

    def __init__(self, config: ToolExecutionConfig):
        self.config = config
        self.workspace = Path(config.workspace).resolve()
        self.command_executor = SandboxedCommandExecutor(
            SandboxedCommandConfig(
                workspace=self.workspace,
                timeout_seconds=config.shell_timeout_seconds,
                max_output_chars=config.max_output_chars,
                text_encoding=config.text_encoding,
                memory_limit_mb=config.memory_limit_mb,
                cpu_time_seconds=config.cpu_time_seconds,
                max_processes=config.max_processes,
                require_network_isolation=config.require_network_isolation,
                require_filesystem_isolation=config.require_filesystem_isolation,
            )
        )

    def resolve_path(self, path: str, cwd: Path | None = None) -> Path:
        """Resolve a path inside the selected execution root.

        A teammate may pass its worktree as ``cwd``; otherwise the project
        workspace is used. Absolute paths are accepted only when they remain
        inside that root.
        """

        base = Path(cwd).resolve() if cwd is not None else self.workspace
        if not base.is_relative_to(self.workspace):
            raise ValueError(f"Execution root escapes workspace: {base}")
        resolved = (base / path).resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(f"Path escapes workspace: {path}")
        return resolved

    def run_bash(
        self,
        command: str,
        cwd: Path | None = None,
        run_in_background: bool = False,
    ) -> str:
        # ``run_in_background`` is consumed by the dispatcher. Direct execution
        # remains synchronous to preserve the existing handler contract.
        del run_in_background
        return self.command_executor.execute(command, cwd=cwd)

    def read_file(
        self,
        path: str,
        limit: int | None = None,
        offset: int = 0,
        cwd: Path | None = None,
    ) -> str:
        try:
            content = self.resolve_path(path, cwd).read_text(
                encoding=self.config.text_encoding,
                errors="replace",
            )
            lines = content.splitlines()
            offset = max(int(offset or 0), 0)
            limit = int(limit) if limit is not None else None
            lines = lines[offset:]
            if limit is not None and limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            return "\n".join(lines)
        except Exception as exc:
            return f"Error: {exc}"

    def write_file(
        self,
        path: str,
        content: str,
        cwd: Path | None = None,
    ) -> str:
        try:
            file_path = self.resolve_path(path, cwd)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding=self.config.text_encoding)
            return f"Wrote {len(content)} bytes to {path}"
        except Exception as exc:
            return f"Error: {exc}"

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        cwd: Path | None = None,
    ) -> str:
        try:
            file_path = self.resolve_path(path, cwd)
            text = file_path.read_text(
                encoding=self.config.text_encoding,
                errors="replace",
            )
            if old_text not in text:
                return f"Error: text not found in {path}"
            file_path.write_text(
                text.replace(old_text, new_text, 1),
                encoding=self.config.text_encoding,
            )
            return f"Edited {path}"
        except Exception as exc:
            return f"Error: {exc}"

    def glob(self, pattern: str, cwd: Path | None = None) -> str:
        try:
            base = Path(cwd).resolve() if cwd is not None else self.workspace
            if not base.is_relative_to(self.workspace):
                raise ValueError(f"Execution root escapes workspace: {base}")
            results: list[str] = []
            for match in globlib.glob(pattern, root_dir=base):
                candidate = (base / match).resolve()
                if candidate.is_relative_to(base):
                    results.append(match)
            return "\n".join(results) if results else "(no matches)"
        except Exception as exc:
            return f"Error: {exc}"

    @staticmethod
    def call_handler(
        handler: Callable[..., Any] | None,
        args: dict[str, Any] | None,
        name: str,
    ) -> str:
        if handler is None:
            return f"Unknown: {name}"
        try:
            return handler(**(args or {}))
        except TypeError as exc:
            return f"Error: {exc}"
