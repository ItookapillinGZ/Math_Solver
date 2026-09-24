from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


VALID_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True)
class WorktreeRuntimeConfig:
    workspace_root: Path
    worktrees_dir: Path
    git_timeout_seconds: int = 30
    status_timeout_seconds: int = 10
    output_limit: int = 5000


@dataclass
class WorktreeRuntimeDependencies:
    load_task: Callable[[str], Any]
    update_task_worktree: Callable[[str, str | None], Any]
    terminal_print: Callable[[str], None] = print
    clock: Callable[[], float] = field(default=time.time)


class WorktreeRuntime:
    """Owns Git worktree lifecycle and task/worktree binding.

    The runtime deliberately knows nothing about TaskStore itself. Task lookup
    and task mutation are injected so the VCS layer stays independently
    testable and does not create a dependency cycle with the task subsystem.
    """

    def __init__(
        self,
        config: WorktreeRuntimeConfig,
        dependencies: WorktreeRuntimeDependencies,
    ) -> None:
        self.config = config
        self.dependencies = dependencies
        self.workspace_root = Path(config.workspace_root).resolve()
        self.worktrees_dir = Path(config.worktrees_dir).resolve()
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_name(name: str) -> str | None:
        if not name:
            return "Worktree name cannot be empty"
        if name in (".", ".."):
            return f"'{name}' is not a valid worktree name"
        if not VALID_WORKTREE_NAME.match(name):
            return (
                f"Invalid worktree name '{name}': "
                "only letters, digits, dots, underscores, dashes (1-64 chars)"
            )
        return None

    def run_git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
    ) -> tuple[bool, str]:
        run_cwd = Path(cwd).resolve() if cwd is not None else self.workspace_root
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=run_cwd,
                capture_output=True,
                text=True,
                timeout=(timeout or self.config.git_timeout_seconds),
            )
            output = (result.stdout + result.stderr).strip()
            if not output:
                output = "(no output)"
            return result.returncode == 0, output[: self.config.output_limit]
        except subprocess.TimeoutExpired:
            return False, "Error: git timeout"

    def log_event(
        self,
        event_type: str,
        worktree_name: str,
        task_id: str = "",
    ) -> None:
        event = {
            "type": event_type,
            "worktree": worktree_name,
            "task_id": task_id,
            "ts": self.dependencies.clock(),
        }
        events_file = self.worktrees_dir / "events.jsonl"
        with events_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def bind_task(self, task_id: str, worktree_name: str) -> None:
        try:
            self.dependencies.update_task_worktree(task_id, worktree_name)
        except KeyError as exc:
            raise FileNotFoundError(task_id) from exc

    def create(self, name: str, task_id: str = "") -> str:
        error = self.validate_name(name)
        if error:
            return f"Error: {error}"

        if task_id:
            try:
                self.dependencies.load_task(task_id)
            except FileNotFoundError:
                return f"Error: task {task_id} not found"

        path = self.worktrees_dir / name
        if path.exists():
            return f"Worktree '{name}' already exists at {path}"

        ok, result = self.run_git(
            ["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"]
        )
        if not ok:
            return f"Git error: {result}"

        if task_id:
            self.bind_task(task_id, name)

        self.log_event("create", name, task_id)
        self.dependencies.terminal_print(
            f"  \033[33m[worktree] created: {name} at {path}\033[0m"
        )
        return f"Worktree '{name}' created at {path}"

    def count_changes(self, path: Path) -> tuple[int, int]:
        path = Path(path).resolve()
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=self.config.status_timeout_seconds,
            )
            files = len(
                [line for line in status.stdout.strip().splitlines() if line.strip()]
            )

            commits_result = subprocess.run(
                ["git", "log", "@{push}..HEAD", "--oneline"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=self.config.status_timeout_seconds,
            )
            commits = len(
                [
                    line
                    for line in commits_result.stdout.strip().splitlines()
                    if line.strip()
                ]
            )
            return files, commits
        except Exception:
            return -1, -1

    def remove(self, name: str, discard_changes: bool = False) -> str:
        error = self.validate_name(name)
        if error:
            return error

        path = self.worktrees_dir / name
        if not path.exists():
            return f"Worktree '{name}' not found"

        if not discard_changes:
            files, commits = self.count_changes(path)
            if files < 0:
                return "Cannot verify status. Use discard_changes=true to force."
            if files > 0 or commits > 0:
                return (
                    f"Worktree '{name}' has {files} file(s), {commits} commit(s). "
                    "Use discard_changes=true or keep_worktree."
                )

        ok, _ = self.run_git(["worktree", "remove", str(path), "--force"])
        if not ok:
            return f"Failed to remove worktree '{name}'"

        # Preserve the teaching runtime's cleanup behavior. Branch deletion is
        # best-effort after the worktree itself has been removed.
        self.run_git(["branch", "-D", f"wt/{name}"])
        self.log_event("remove", name)
        self.dependencies.terminal_print(
            f"  \033[33m[worktree] removed: {name}\033[0m"
        )
        return f"Worktree '{name}' removed"

    def keep(self, name: str) -> str:
        error = self.validate_name(name)
        if error:
            return error
        self.log_event("keep", name)
        return f"Worktree '{name}' kept for review (branch: wt/{name})"
