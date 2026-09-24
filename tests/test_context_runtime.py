import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime.context import (
    ContextRuntime,
    ContextRuntimeConfig,
    ContextRuntimeDependencies,
)
from agent_runtime.memory import MemoryKind, MemoryStore


CHECKPOINT_JSON = json.dumps({
    "current_goal": "prove theorem",
    "user_constraints": ["do not assume beta constant"],
    "completed_work": ["derived scalar equation"],
    "active_tasks": ["task_math_1"],
    "key_artifacts": ["PROOF/main.tex"],
    "important_decisions": ["use eigenfunction expansion"],
    "remaining_work": ["verify slope sign"],
})


class ContextRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root / "memory.db")
        self.scope_id = "scope_test"
        self.store.create_scope("test", scope_id=self.scope_id)
        self.logs = []
        self.summary_prompts = []

    def tearDown(self):
        self.tmp.cleanup()

    def runtime(self, *, token_limit=1000, persist_threshold=20, summary=CHECKPOINT_JSON):
        def summarize(prompt):
            self.summary_prompts.append(prompt)
            if isinstance(summary, Exception):
                raise summary
            return summary

        return ContextRuntime(
            ContextRuntimeConfig(
                transcript_dir=self.root / "transcripts",
                tool_results_dir=self.root / "tool_outputs",
                persist_threshold=persist_threshold,
                token_limit=token_limit,
                keep_recent_tool_results=0,
                soft_limit_ratio=0.8,
                tool_result_compact_threshold_tokens=5,
                tool_result_max_bytes=40,
            ),
            ContextRuntimeDependencies(
                summarize_checkpoint=summarize,
                memory_store=self.store,
                get_active_scope_id=lambda: self.scope_id,
                terminal_print=self.logs.append,
            ),
        )

    def test_small_output_is_not_persisted(self):
        runtime = self.runtime(persist_threshold=50)
        self.assertEqual(runtime.persist_large_output("tool1", "short"), "short")
        self.assertFalse((self.root / "tool_outputs").exists())

    def test_large_output_is_persisted_and_referenced(self):
        runtime = self.runtime(persist_threshold=5)
        reference = runtime.persist_large_output("tool1", "abcdefghij")
        path = self.root / "tool_outputs" / "tool1.txt"
        self.assertEqual(path.read_text(), "abcdefghij")
        self.assertIn(str(path), reference)
        self.assertIn("Preview:", reference)

    def test_tool_result_budget_persists_largest_current_results(self):
        runtime = self.runtime(persist_threshold=5)
        messages = [{
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "A" * 60},
                {"type": "tool_result", "tool_use_id": "b", "content": "B" * 10},
            ],
        }]
        runtime.tool_result_budget(messages, max_bytes=30)
        self.assertIn("<persisted-output>", messages[0]["content"][0]["content"])
        self.assertTrue((self.root / "tool_outputs" / "a.txt").exists())

    def test_write_transcript_writes_jsonl(self):
        runtime = self.runtime()
        messages = [{"role": "user", "content": "hello"}]
        path = runtime.write_transcript(messages)
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text().splitlines()[0]), messages[0])

    def test_compact_history_projects_checkpoint_into_scoped_memory(self):
        runtime = self.runtime()
        result = runtime.compact_history([{"role": "user", "content": "history"}])
        self.assertEqual(len(result), 1)
        self.assertIn("Structured Compaction Checkpoint", result[0]["content"])
        self.assertEqual(len(self.summary_prompts), 1)

        working = self.store.list_memories(
            kind=MemoryKind.WORKING,
            scope_ids=[self.scope_id],
            limit=20,
        )
        by_key = {record.key: record.content for record in working}
        self.assertEqual(by_key["current_goal"], "prove theorem")
        self.assertIn("do not assume beta constant", by_key["user_constraints"])

        episodes = self.store.list_memories(
            kind=MemoryKind.EPISODIC,
            scope_ids=[self.scope_id],
            limit=10,
        )
        self.assertTrue(any("derived scalar equation" in item.content for item in episodes))

        artifacts = self.store.list_memories(
            kind=MemoryKind.ARTIFACT,
            scope_ids=[self.scope_id],
            limit=10,
        )
        self.assertTrue(any(item.metadata.get("artifact_type") == "transcript" for item in artifacts))

    def test_compact_history_falls_back_when_summary_fails(self):
        runtime = self.runtime(summary=RuntimeError("model unavailable"))
        result = runtime.compact_history([{"role": "user", "content": "history"}])
        self.assertIn("Structured checkpoint extraction failed", result[0]["content"])
        self.assertTrue(any("structured checkpoint fallback" in line for line in self.logs))

    def test_reactive_compact_keeps_recent_five_messages(self):
        runtime = self.runtime()
        messages = [{"role": "user", "content": f"m{i}"} for i in range(8)]
        result = runtime.reactive_compact(messages)
        self.assertEqual(len(result), 6)
        self.assertIn("Reactive Structured Compaction Checkpoint", result[0]["content"])
        self.assertEqual(result[1:], messages[-5:])

    def test_prepare_context_compacts_old_tool_results_before_summary(self):
        runtime = self.runtime(token_limit=140, persist_threshold=10_000)
        messages = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "old", "content": "x" * 600},
            ]},
            {"role": "assistant", "content": "recent answer"},
        ]
        runtime.prepare_context(messages)
        self.assertIn("Earlier tool result compacted", messages[0]["content"][0]["content"])
        self.assertEqual(self.summary_prompts, [])
        self.assertTrue(any("[context] compacted" in line for line in self.logs))

    def test_prepare_context_uses_semantic_compaction_when_still_over_budget(self):
        runtime = self.runtime(token_limit=40, persist_threshold=10_000)
        messages = [{"role": "user", "content": "important user constraint " * 80}]
        runtime.prepare_context(messages)
        self.assertEqual(len(messages), 1)
        self.assertIn("Structured Compaction Checkpoint", messages[0]["content"])
        self.assertEqual(len(self.summary_prompts), 1)
        self.assertTrue(any("creating semantic summary" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
