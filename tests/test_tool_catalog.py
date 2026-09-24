import unittest

from agent_runtime.tools import (
    BASIC_TOOL_CATALOG,
    ToolCatalogEntry,
    build_basic_tool_registry,
    catalog_names,
)


EXPECTED_TOOL_NAMES = ['bash',
 'read_file',
 'write_file',
 'edit_file',
 'glob',
 'search_literature',
 'download_arxiv_source',
 'view_latex_theorem',
 'todo_write',
 'task',
 'load_skill',
 'create_task',
 'list_tasks',
 'get_task',
 'claim_task',
 'complete_task',
 'heartbeat_task',
 'recover_expired_tasks',
 'schedule_cron',
 'list_crons',
 'cancel_cron',
 'spawn_teammate',
 'send_message',
 'check_inbox',
 'request_shutdown',
 'request_plan',
 'review_plan',
 'create_worktree',
 'remove_worktree',
 'keep_worktree',
 'connect_mcp',
 'research_workflow',
 'list_background_jobs',
 'get_background_job',
 'memory_set_working',
 'memory_add_episode',
 'memory_add_artifact',
 'list_memories',
 'search_memories',
 'memory_start_scope',
 'memory_switch_scope',
 'memory_list_scopes',
 'memory_promote_global']


def _handler_factory(name):
    def handler(**kwargs):
        return {"name": name, "kwargs": kwargs}
    return handler


class ToolCatalogTests(unittest.TestCase):
    def setUp(self):
        self.handlers = {name: _handler_factory(name) for name in EXPECTED_TOOL_NAMES}

    def test_catalog_preserves_all_builtin_names_and_order(self):
        self.assertEqual(list(catalog_names()), EXPECTED_TOOL_NAMES)
        self.assertEqual(len(BASIC_TOOL_CATALOG), 43)

    def test_catalog_names_are_unique(self):
        names = catalog_names()
        self.assertEqual(len(names), len(set(names)))

    def test_builder_binds_every_handler_in_catalog_order(self):
        registry = build_basic_tool_registry(self.handlers)
        self.assertEqual(registry.names(), EXPECTED_TOOL_NAMES)
        for name in EXPECTED_TOOL_NAMES:
            self.assertIs(registry.get_handler(name), self.handlers[name])

    def test_builder_rejects_missing_handler(self):
        handlers = dict(self.handlers)
        handlers.pop("bash")
        with self.assertRaisesRegex(KeyError, "bash"):
            build_basic_tool_registry(handlers)

    def test_builder_rejects_non_callable_handler(self):
        handlers = dict(self.handlers)
        handlers["bash"] = "not-callable"
        with self.assertRaisesRegex(TypeError, "bash"):
            build_basic_tool_registry(handlers)

    def test_critical_capabilities_are_preserved(self):
        registry = build_basic_tool_registry(self.handlers)
        self.assertEqual(registry.capabilities_for("bash"), frozenset({"shell.execute"}))
        self.assertEqual(
            registry.capabilities_for("edit_file"),
            frozenset({"filesystem.read", "filesystem.write"}),
        )
        self.assertEqual(
            registry.capabilities_for("download_arxiv_source"),
            frozenset({"filesystem.write", "network.access"}),
        )
        self.assertEqual(registry.capabilities_for("task"), frozenset({"agent.spawn"}))
        self.assertEqual(
            registry.capabilities_for("connect_mcp"),
            frozenset({"mcp.connect", "network.access"}),
        )
        self.assertEqual(
            registry.capabilities_for("research_workflow"),
            frozenset({"research.workflow.write", "task.write", "agent.spawn"}),
        )
        self.assertEqual(
            registry.capabilities_for("memory_promote_global"),
            frozenset({"memory.write"}),
        )

    def test_api_schema_does_not_expose_runtime_metadata(self):
        registry = build_basic_tool_registry(self.handlers)
        schema = next(item for item in registry.schemas() if item["name"] == "bash")
        self.assertEqual(set(schema), {"name", "description", "input_schema"})
        self.assertNotIn("capabilities", schema)
        self.assertNotIn("handler", schema)

    def test_affinity_and_memory_schemas_are_preserved(self):
        registry = build_basic_tool_registry(self.handlers)
        create_task = registry.get("create_task")
        self.assertEqual(
            create_task.input_schema["properties"]["task_type"]["enum"],
            ["general", "math", "engineering", "literature"],
        )
        self.assertIn("required_roles", create_task.input_schema["properties"])

        search_memories = registry.get("search_memories")
        self.assertEqual(
            search_memories.input_schema["properties"]["scope"]["enum"],
            ["current", "global", "all"],
        )
        self.assertEqual(search_memories.input_schema["required"], ["query"])

    def test_bound_schema_is_a_copy_of_catalog_metadata(self):
        registry = build_basic_tool_registry(self.handlers)
        tool = registry.get("bash")
        entry = next(item for item in BASIC_TOOL_CATALOG if item.name == "bash")
        self.assertIsNot(tool.input_schema, entry.input_schema)
        tool.input_schema["properties"]["extra"] = {"type": "string"}
        self.assertNotIn("extra", entry.input_schema["properties"])


if __name__ == "__main__":
    unittest.main()
