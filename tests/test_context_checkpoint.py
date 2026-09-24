import json
import unittest

from agent_runtime.context import (
    CompactionCheckpoint,
    build_checkpoint_prompt,
    parse_checkpoint_response,
)


class CompactionCheckpointTests(unittest.TestCase):
    def test_parse_structured_checkpoint(self):
        raw = json.dumps({
            "current_goal": "Finish the runtime refactor",
            "user_constraints": ["Do not rewrite everything at once"],
            "completed_work": ["Added JobStore"],
            "active_tasks": ["Step 7B"],
            "key_artifacts": ["s20.py"],
            "important_decisions": ["Use SQLite for jobs"],
            "remaining_work": ["Add structured checkpoints"],
        })
        checkpoint = parse_checkpoint_response(
            raw,
            transcript_path=".transcripts/t1.jsonl",
        )
        self.assertEqual(checkpoint.current_goal, "Finish the runtime refactor")
        self.assertEqual(
            checkpoint.user_constraints,
            ("Do not rewrite everything at once",),
        )

    def test_runtime_transcript_path_overrides_model_value(self):
        raw = json.dumps({
            "transcript_path": "C:/malicious/or/hallucinated/path",
            "current_goal": "Keep working",
        })
        checkpoint = parse_checkpoint_response(
            raw,
            transcript_path=".transcripts/real.jsonl",
        )
        self.assertEqual(
            checkpoint.transcript_path,
            ".transcripts/real.jsonl",
        )

    def test_parser_accepts_markdown_fence(self):
        raw = '```json\n{"current_goal":"Continue","remaining_work":["test"]}\n```'
        checkpoint = parse_checkpoint_response(
            raw,
            transcript_path="t.jsonl",
        )
        self.assertEqual(checkpoint.current_goal, "Continue")
        self.assertEqual(checkpoint.remaining_work, ("test",))

    def test_parser_rejects_non_json(self):
        with self.assertRaises(ValueError):
            parse_checkpoint_response(
                "This is only a prose summary.",
                transcript_path="t.jsonl",
            )

    def test_context_text_contains_structured_fields_and_transcript(self):
        checkpoint = CompactionCheckpoint(
            transcript_path=".transcripts/t2.jsonl",
            current_goal="Goal",
            user_constraints=("Keep constraint",),
            remaining_work=("Next step",),
        )
        text = checkpoint.to_context_text()
        self.assertIn("Structured Compaction Checkpoint", text)
        self.assertIn(".transcripts/t2.jsonl", text)
        self.assertIn("Keep constraint", text)
        self.assertIn("Next step", text)

    def test_fallback_checkpoint_keeps_transcript_pointer(self):
        checkpoint = CompactionCheckpoint.fallback(
            transcript_path=".transcripts/fallback.jsonl",
            note="Reactive compaction fallback",
        )
        self.assertEqual(
            checkpoint.transcript_path,
            ".transcripts/fallback.jsonl",
        )
        self.assertTrue(checkpoint.remaining_work)

    def test_checkpoint_prompt_requests_required_schema(self):
        prompt = build_checkpoint_prompt([
            {"role": "user", "content": "Do not delete important constraints."}
        ])
        for field in (
            "current_goal",
            "user_constraints",
            "completed_work",
            "active_tasks",
            "key_artifacts",
            "important_decisions",
            "remaining_work",
        ):
            self.assertIn(field, prompt)
        self.assertIn("Do not delete important constraints", prompt)


if __name__ == "__main__":
    unittest.main()
