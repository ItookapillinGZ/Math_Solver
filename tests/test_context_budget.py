import unittest

from agent_runtime.context import ContextBudgetManager, HeuristicTokenEstimator


class ContextBudgetManagerTests(unittest.TestCase):
    def test_estimator_counts_structured_messages(self):
        estimator = HeuristicTokenEstimator()
        messages = [
            {"role": "user", "content": "Explain this result."},
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "read_file", "input": {"path": "a.py"}}
            ]},
        ]
        self.assertGreater(estimator.estimate_messages(messages), 0)

    def test_non_ascii_text_is_weighted_more_densely_than_ascii(self):
        estimator = HeuristicTokenEstimator()
        ascii_tokens = estimator.estimate_text("a" * 100)
        cjk_tokens = estimator.estimate_text("数" * 100)
        self.assertGreater(cjk_tokens, ascii_tokens)

    def test_under_soft_limit_is_not_modified(self):
        manager = ContextBudgetManager(token_limit=1000)
        messages = [{"role": "user", "content": "short request"}]
        prepared, report = manager.fit(messages)
        self.assertEqual(prepared, messages)
        self.assertEqual(report.compacted_tool_results, 0)
        self.assertFalse(report.needs_summary)

    def test_old_large_tool_results_compact_before_recent_results(self):
        manager = ContextBudgetManager(
            token_limit=500,
            soft_limit_ratio=0.8,
            keep_recent_tool_results=1,
            tool_result_compact_threshold_tokens=20,
        )
        old_output = "old-output-" * 250
        recent_output = "recent-output-" * 120
        messages = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "old", "content": old_output}
            ]},
            {"role": "assistant", "content": "continue"},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "recent", "content": recent_output}
            ]},
        ]

        prepared, report = manager.fit(messages)

        self.assertEqual(report.compacted_tool_results, 1)
        self.assertIn("Earlier tool result compacted", prepared[0]["content"][0]["content"])
        self.assertEqual(prepared[2]["content"][0]["content"], recent_output)

    def test_user_instructions_are_never_locally_dropped(self):
        manager = ContextBudgetManager(
            token_limit=200,
            soft_limit_ratio=0.5,
            keep_recent_tool_results=0,
            tool_result_compact_threshold_tokens=10,
        )
        instruction = "Never modify files outside the workspace. " * 60
        messages = [
            {"role": "user", "content": instruction},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 1000}
            ]},
        ]

        prepared, _ = manager.fit(messages)
        self.assertEqual(prepared[0]["content"], instruction)

    def test_summary_requested_when_high_priority_content_still_exceeds_limit(self):
        manager = ContextBudgetManager(token_limit=100, soft_limit_ratio=0.8)
        messages = [{"role": "user", "content": "important constraint " * 400}]
        _, report = manager.fit(messages)
        self.assertTrue(report.needs_summary)

    def test_input_messages_are_not_mutated(self):
        manager = ContextBudgetManager(
            token_limit=150,
            soft_limit_ratio=0.5,
            keep_recent_tool_results=0,
            tool_result_compact_threshold_tokens=10,
        )
        original = "tool output " * 200
        messages = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": original}
        ]}]
        prepared, _ = manager.fit(messages)
        self.assertEqual(messages[0]["content"][0]["content"], original)
        self.assertNotEqual(prepared[0]["content"][0]["content"], original)


if __name__ == "__main__":
    unittest.main()
