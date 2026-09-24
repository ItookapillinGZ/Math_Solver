import unittest
from types import SimpleNamespace

from agent_runtime.runtime import (
    AnthropicGatewayConfig,
    AnthropicModelGateway,
)


class RateLimitError(Exception):
    pass


class OverloadedError(Exception):
    pass


class FakeMessagesAPI:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.messages = FakeMessagesAPI(outcomes)


class AnthropicModelGatewayTests(unittest.TestCase):
    def make_gateway(self, outcomes, **config_overrides):
        outputs = []
        sleeps = []
        client = FakeClient(outcomes)
        config = AnthropicGatewayConfig(
            primary_model=config_overrides.get("primary_model", "claude-primary"),
            fallback_model=config_overrides.get("fallback_model", "claude-fallback"),
            max_retries=config_overrides.get("max_retries", 3),
            max_consecutive_529=config_overrides.get("max_consecutive_529", 2),
            base_delay_ms=config_overrides.get("base_delay_ms", 100),
        )
        gateway = AnthropicModelGateway(
            client=client,
            prompt_builder=lambda context: f"system:{context.get('topic', '')}",
            config=config,
            output=outputs.append,
            sleep=sleeps.append,
            jitter=lambda low, high: 0.0,
        )
        return gateway, client, outputs, sleeps

    def test_new_state_starts_on_primary_model(self):
        gateway, _, _, _ = self.make_gateway([])
        state = gateway.new_recovery_state()
        self.assertEqual(state.current_model, "claude-primary")
        self.assertFalse(state.has_escalated)
        self.assertEqual(state.recovery_count, 0)

    def test_call_builds_anthropic_request(self):
        response = SimpleNamespace(stop_reason="end_turn", content=[])
        gateway, client, _, _ = self.make_gateway([response])
        state = gateway.new_recovery_state()
        messages = [{"role": "user", "content": "hello"}]
        tools = [{"name": "echo"}]

        actual = gateway.call(messages, {"topic": "proof"}, tools, state, 512)

        self.assertIs(actual, response)
        call = client.messages.calls[0]
        self.assertEqual(call["model"], "claude-primary")
        self.assertEqual(call["system"], "system:proof")
        self.assertIs(call["messages"], messages)
        self.assertIs(call["tools"], tools)
        self.assertEqual(call["max_tokens"], 512)

    def test_sanitize_tool_inputs_repairs_json_string_and_empty_list(self):
        response = SimpleNamespace(stop_reason="end_turn", content=[])
        gateway, _, _, _ = self.make_gateway([response])
        state = gateway.new_recovery_state()
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "a", "input": '{"x": 1}'},
                {"type": "tool_use", "name": "b", "input": []},
            ],
        }]

        gateway.call(messages, {}, [], state, 128)

        self.assertEqual(messages[0]["content"][0]["input"], {"x": 1})
        self.assertEqual(messages[0]["content"][1]["input"], {})

    def test_rate_limit_retries_then_succeeds(self):
        response = SimpleNamespace(stop_reason="end_turn", content=[])
        gateway, client, outputs, sleeps = self.make_gateway([
            RateLimitError("429 rate limited"),
            response,
        ])
        state = gateway.new_recovery_state()

        actual = gateway.call([], {}, [], state, 128)

        self.assertIs(actual, response)
        self.assertEqual(len(client.messages.calls), 2)
        self.assertEqual(sleeps, [0.1])
        self.assertTrue(any("[429] retry 1/3" in line for line in outputs))

    def test_repeated_529_switches_to_fallback_model(self):
        response = SimpleNamespace(stop_reason="end_turn", content=[])
        gateway, client, outputs, _ = self.make_gateway([
            OverloadedError("529 overloaded"),
            OverloadedError("529 overloaded"),
            response,
        ])
        state = gateway.new_recovery_state()

        gateway.call([], {}, [], state, 128)

        self.assertEqual(
            [call["model"] for call in client.messages.calls],
            ["claude-primary", "claude-primary", "claude-fallback"],
        )
        self.assertEqual(state.current_model, "claude-fallback")
        self.assertTrue(any("switching to claude-fallback" in line for line in outputs))

    def test_non_retryable_error_propagates(self):
        gateway, client, _, sleeps = self.make_gateway([ValueError("bad request")])
        state = gateway.new_recovery_state()

        with self.assertRaisesRegex(ValueError, "bad request"):
            gateway.call([], {}, [], state, 128)

        self.assertEqual(len(client.messages.calls), 1)
        self.assertEqual(sleeps, [])

    def test_retry_exhaustion_raises_runtime_error(self):
        gateway, client, _, _ = self.make_gateway(
            [RateLimitError("429"), RateLimitError("429")],
            max_retries=2,
        )
        state = gateway.new_recovery_state()

        with self.assertRaisesRegex(RuntimeError, r"Max retries \(2\) exceeded"):
            gateway.call([], {}, [], state, 128)

        self.assertEqual(len(client.messages.calls), 2)

    def test_prompt_too_long_detection(self):
        gateway, _, _, _ = self.make_gateway([])
        self.assertTrue(gateway.is_prompt_too_long_error(Exception("prompt too long")))
        self.assertTrue(gateway.is_prompt_too_long_error(Exception("context_length_exceeded")))
        self.assertTrue(gateway.is_prompt_too_long_error(Exception("max_context_window")))
        self.assertFalse(gateway.is_prompt_too_long_error(Exception("network failed")))


if __name__ == "__main__":
    unittest.main()
