import unittest

from agent_runtime.tools import ToolDefinition, ToolRegistry


def _handler(**_kwargs):
    return "ok"


class ToolCapabilityTests(unittest.TestCase):
    def test_capabilities_are_normalized_and_not_sent_to_model(self):
        tool = ToolDefinition(
            name="writer",
            description="writes",
            input_schema={"type": "object", "properties": {}},
            handler=_handler,
            capabilities={"filesystem.write", "filesystem.read"},
        )
        self.assertEqual(
            tool.capabilities,
            frozenset({"filesystem.write", "filesystem.read"}),
        )
        self.assertNotIn("capabilities", tool.to_api_schema())

    def test_registry_returns_declared_capabilities(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="reader",
                description="reads",
                input_schema={"type": "object", "properties": {}},
                handler=_handler,
                capabilities={"filesystem.read"},
            )
        )
        self.assertEqual(
            registry.capabilities_for("reader"),
            frozenset({"filesystem.read"}),
        )
        self.assertEqual(registry.capabilities_for("missing"), frozenset())


if __name__ == "__main__":
    unittest.main()
