import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_runtime.worktree import (
    WorktreeRuntime,
    WorktreeRuntimeConfig,
    WorktreeRuntimeDependencies,
)


@unittest.skipUnless(shutil.which("git"), "git is required")
class WorktreeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.worktrees = self.root / ".worktrees"
        self.tasks = {"task_math": {"id": "task_math", "worktree": None}}
        self.bound = []
        self.logs = []

        self._git("init")
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "Runtime Tests")
        (self.root / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
        (self.root / "README.md").write_text("base\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "initial")

        def load_task(task_id):
            if task_id not in self.tasks:
                raise FileNotFoundError(task_id)
            return self.tasks[task_id]

        def update_task_worktree(task_id, worktree):
            if task_id not in self.tasks:
                raise KeyError(task_id)
            self.tasks[task_id]["worktree"] = worktree
            self.bound.append((task_id, worktree))
            return self.tasks[task_id]

        self.runtime = WorktreeRuntime(
            WorktreeRuntimeConfig(
                workspace_root=self.root,
                worktrees_dir=self.worktrees,
            ),
            WorktreeRuntimeDependencies(
                load_task=load_task,
                update_task_worktree=update_task_worktree,
                terminal_print=self.logs.append,
                clock=lambda: 1234.5,
            ),
        )

    def tearDown(self):
        # Prune first so Windows does not retain worktree metadata pointing at
        # directories TemporaryDirectory is trying to delete.
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.tempdir.cleanup()

    def _git(self, *args, cwd=None):
        result = subprocess.run(
            ["git", *args],
            cwd=(cwd or self.root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.fail(
                f"git {' '.join(args)} failed: {result.stdout}\n{result.stderr}"
            )
        return result.stdout.strip()

    def test_validate_name_rejects_unsafe_names(self):
        self.assertIsNone(self.runtime.validate_name("proof_worker-1"))
        self.assertIsNotNone(self.runtime.validate_name("../escape"))
        self.assertIsNotNone(self.runtime.validate_name("bad/name"))
        self.assertIsNotNone(self.runtime.validate_name(""))

    def test_create_worktree_binds_task_and_records_event(self):
        result = self.runtime.create("proof-a", "task_math")
        path = self.worktrees / "proof-a"

        self.assertIn("created", result)
        self.assertTrue(path.exists())
        self.assertEqual(self.tasks["task_math"]["worktree"], "proof-a")
        self.assertEqual(self.bound, [("task_math", "proof-a")])
        self.assertEqual(self._git("branch", "--show-current", cwd=path), "wt/proof-a")

        events = [
            json.loads(line)
            for line in (self.worktrees / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(events[-1]["type"], "create")
        self.assertEqual(events[-1]["task_id"], "task_math")
        self.assertEqual(events[-1]["ts"], 1234.5)
        self.assertTrue(any("[worktree] created" in line for line in self.logs))

    def test_create_rejects_missing_task_before_git_mutation(self):
        result = self.runtime.create("orphan", "missing")
        self.assertEqual(result, "Error: task missing not found")
        self.assertFalse((self.worktrees / "orphan").exists())
        branches = self._git("branch", "--list", "wt/orphan")
        self.assertEqual(branches, "")

    def test_count_changes_reports_dirty_files(self):
        self.runtime.create("dirty-count")
        path = self.worktrees / "dirty-count"
        (path / "new.txt").write_text("changed\n", encoding="utf-8")

        files, commits = self.runtime.count_changes(path)
        self.assertGreaterEqual(files, 1)
        self.assertGreaterEqual(commits, 0)

    def test_remove_refuses_dirty_worktree_without_discard(self):
        self.runtime.create("dirty")
        path = self.worktrees / "dirty"
        (path / "new.txt").write_text("changed\n", encoding="utf-8")

        refused = self.runtime.remove("dirty")
        self.assertIn("has 1 file(s)", refused)
        self.assertTrue(path.exists())

        removed = self.runtime.remove("dirty", discard_changes=True)
        self.assertEqual(removed, "Worktree 'dirty' removed")
        self.assertFalse(path.exists())
        self.assertEqual(self._git("branch", "--list", "wt/dirty"), "")

    def test_remove_clean_worktree_deletes_branch_and_logs(self):
        self.runtime.create("clean")
        path = self.worktrees / "clean"
        self.assertTrue(path.exists())

        result = self.runtime.remove("clean")

        self.assertEqual(result, "Worktree 'clean' removed")
        self.assertFalse(path.exists())
        self.assertEqual(self._git("branch", "--list", "wt/clean"), "")
        events = (self.worktrees / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"type": "remove"', events)
        self.assertTrue(any("[worktree] removed" in line for line in self.logs))

    def test_keep_preserves_worktree_and_records_event(self):
        self.runtime.create("review")
        path = self.worktrees / "review"

        result = self.runtime.keep("review")

        self.assertIn("kept for review", result)
        self.assertTrue(path.exists())
        events = (self.worktrees / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"type": "keep"', events)

    def test_bind_task_translates_store_key_error(self):
        with self.assertRaises(FileNotFoundError):
            self.runtime.bind_task("missing", "proof")

    def test_run_git_preserves_success_and_failure_contract(self):
        ok, output = self.runtime.run_git(["status", "--short"])
        self.assertTrue(ok)
        self.assertIsInstance(output, str)

        ok, output = self.runtime.run_git(["definitely-not-a-git-subcommand"])
        self.assertFalse(ok)
        self.assertTrue(output)


if __name__ == "__main__":
    unittest.main()
