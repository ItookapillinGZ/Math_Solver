from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.research.mcp import (
    MCPConfigurationError,
    MCPRuntime,
    MCPSDKBindings,
    MCPServerSpec,
    MockMCPRuntime,
    PersistentMCPClient,
    load_mcp_server_specs,
)


class FakeTool:
    def __init__(self, name: str, description: str = "", schema: dict | None = None):
        self.name = name
        self.description = description
        self.input_schema = schema or {"type": "object", "properties": {}}


class FakeText:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class FakeStdioServerParameters:
    created: list["FakeStdioServerParameters"] = []

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.__class__.created.append(self)


class FakeTimeout:
    created: list[tuple[float, float | None]] = []

    def __init__(self, default: float, *, read: float | None = None):
        self.default = default
        self.read = read
        self.__class__.created.append((default, read))


class FakeHTTPClient:
    created: list["FakeHTTPClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.entered = False
        self.closed = False
        self.__class__.created.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_):
        self.closed = True
        return False


class FakeHTTPTransport:
    def __init__(self, url: str, http_client, terminate_on_close: bool):
        self.url = url
        self.http_client = http_client
        self.terminate_on_close = terminate_on_close


HTTP_TRANSPORTS: list[FakeHTTPTransport] = []


def fake_streamable_http_client(url: str, *, http_client, terminate_on_close: bool = True):
    transport = FakeHTTPTransport(url, http_client, terminate_on_close)
    HTTP_TRANSPORTS.append(transport)
    return transport


class FakeClient:
    tools = [
        FakeTool(
            "add",
            "Add two integers",
            {
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
        )
    ]
    enter_count = 0
    exit_count = 0
    call_count = 0
    servers: list[object] = []

    def __init__(self, server):
        self.server = server
        self.protocol_version = "2026-07-28"
        self.server_info = SimpleNamespace(name="fake", version="2.0")
        self.__class__.servers.append(server)

    async def __aenter__(self):
        self.__class__.enter_count += 1
        return self

    async def __aexit__(self, *_):
        self.__class__.exit_count += 1
        return False

    async def list_tools(self, cursor=None):
        return SimpleNamespace(tools=list(self.tools), next_cursor=None)

    async def call_tool(self, name: str, arguments: dict):
        self.__class__.call_count += 1
        value = arguments.get("a", 0) + arguments.get("b", 0)
        return SimpleNamespace(
            content=[FakeText(str(value))],
            structured_content={"result": value},
            is_error=False,
        )


class FailingOnceClient(FakeClient):
    fail_next = True
    call_count = 0
    enter_count = 0
    exit_count = 0
    servers = []

    async def call_tool(self, name: str, arguments: dict):
        self.__class__.call_count += 1
        if self.__class__.fail_next:
            self.__class__.fail_next = False
            raise ConnectionError("wire dropped")
        value = arguments.get("a", 0) + arguments.get("b", 0)
        return SimpleNamespace(
            content=[FakeText(str(value))],
            structured_content={"result": value},
            is_error=False,
        )


class ManyToolsClient(FakeClient):
    tools = [FakeTool(f"tool_{i}") for i in range(3)]
    enter_count = 0
    exit_count = 0
    call_count = 0
    servers = []


class CollisionToolsClient(FakeClient):
    tools = [FakeTool("a.b"), FakeTool("a_b")]
    enter_count = 0
    exit_count = 0
    call_count = 0
    servers = []


def fake_sdk_loader(client_cls=FakeClient) -> MCPSDKBindings:
    return MCPSDKBindings(
        client_cls=client_cls,
        stdio_parameters_cls=FakeStdioServerParameters,
        streamable_http_client=fake_streamable_http_client,
        async_http_client_cls=FakeHTTPClient,
        http_timeout_cls=FakeTimeout,
    )


class MCPPersistentTransportTests(unittest.TestCase):
    def setUp(self):
        FakeClient.enter_count = 0
        FakeClient.exit_count = 0
        FakeClient.call_count = 0
        FakeClient.servers.clear()
        FailingOnceClient.fail_next = True
        FailingOnceClient.enter_count = 0
        FailingOnceClient.exit_count = 0
        FailingOnceClient.call_count = 0
        FailingOnceClient.servers.clear()
        FakeStdioServerParameters.created.clear()
        FakeHTTPClient.created.clear()
        FakeTimeout.created.clear()
        HTTP_TRANSPORTS.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.trusted = self.root / "trusted"
        self.trusted.mkdir()
        self.runtimes: list[MCPRuntime] = []

    def tearDown(self):
        for runtime in reversed(self.runtimes):
            runtime.close()
        self.temp_dir.cleanup()

    def write_config(self, payload: dict) -> Path:
        path = self.trusted / "mcp_servers.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def runtime(self, payload: dict, *, client_cls=FakeClient, fallback=None) -> MCPRuntime:
        path = self.write_config(payload)
        runtime = MCPRuntime(
            path,
            workspace=self.workspace,
            terminal_print=lambda *_: None,
            fallback_runtime=fallback,
            sdk_loader=lambda: fake_sdk_loader(client_cls),
        )
        self.runtimes.append(runtime)
        return runtime

    def test_loads_stdio_and_streamable_http_specs(self):
        path = self.write_config(
            {
                "servers": {
                    "local": {"command": "python", "args": ["--serve"]},
                    "remote": {
                        "transport": "streamable-http",
                        "url": "https://mcp.example.com/mcp",
                        "headers": {"X-Tenant": "research"},
                        "bearer_token_env": "MCP_TOKEN",
                    },
                }
            }
        )
        specs = load_mcp_server_specs(path)
        self.assertEqual(specs["local"].transport, "stdio")
        self.assertEqual(specs["remote"].transport, "streamable-http")
        self.assertEqual(specs["remote"].url, "https://mcp.example.com/mcp")
        self.assertEqual(specs["remote"].headers["X-Tenant"], "research")

    def test_insecure_remote_http_requires_explicit_trusted_opt_in(self):
        path = self.write_config(
            {
                "servers": {
                    "remote": {
                        "transport": "streamable-http",
                        "url": "http://mcp.example.com/mcp",
                    }
                }
            }
        )
        with self.assertRaises(MCPConfigurationError):
            load_mcp_server_specs(path)

    def test_loopback_http_is_allowed_for_local_development(self):
        spec = MCPServerSpec(
            name="local-http",
            transport="streamable-http",
            url="http://127.0.0.1:8000/mcp",
        )
        self.assertEqual(spec.transport, "streamable-http")

    def test_persistent_stdio_connection_is_reused_across_tool_calls(self):
        runtime = self.runtime(
            {"servers": {"math": {"command": "python", "args": ["--serve"]}}}
        )
        self.assertIn("via stdio", runtime.connect("math"))
        tools, handlers = runtime.assemble_tool_pool([], [], {})
        self.assertEqual(tools[0]["name"], "mcp__math__add")
        self.assertIn("3", handlers["mcp__math__add"](a=1, b=2))
        self.assertIn("9", handlers["mcp__math__add"](a=4, b=5))
        self.assertEqual(FakeClient.enter_count, 1)
        self.assertEqual(FakeClient.call_count, 2)
        self.assertEqual(len(FakeStdioServerParameters.created), 1)

    def test_persistent_http_uses_custom_headers_token_and_timeout(self):
        old = os.environ.get("MCP_TEST_TOKEN")
        os.environ["MCP_TEST_TOKEN"] = "secret-token"
        try:
            runtime = self.runtime(
                {
                    "servers": {
                        "remote": {
                            "transport": "streamable-http",
                            "url": "https://mcp.example.com/mcp",
                            "headers": {"X-Tenant": "research"},
                            "bearer_token_env": "MCP_TEST_TOKEN",
                            "timeout_seconds": 12,
                            "read_timeout_seconds": 90,
                            "terminate_on_close": False,
                        }
                    }
                }
            )
            output = runtime.connect("remote")
            self.assertIn("via streamable-http", output)
            self.assertEqual(len(FakeHTTPClient.created), 1)
            kwargs = FakeHTTPClient.created[0].kwargs
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-token")
            self.assertEqual(kwargs["headers"]["X-Tenant"], "research")
            self.assertEqual(FakeTimeout.created[-1], (12.0, 90.0))
            self.assertEqual(HTTP_TRANSPORTS[0].url, "https://mcp.example.com/mcp")
            self.assertFalse(HTTP_TRANSPORTS[0].terminate_on_close)
        finally:
            if old is None:
                os.environ.pop("MCP_TEST_TOKEN", None)
            else:
                os.environ["MCP_TEST_TOKEN"] = old

    def test_missing_bearer_environment_variable_fails_without_leaking_secret(self):
        os.environ.pop("MCP_MISSING_TOKEN", None)
        runtime = self.runtime(
            {
                "servers": {
                    "remote": {
                        "transport": "streamable-http",
                        "url": "https://mcp.example.com/mcp",
                        "bearer_token_env": "MCP_MISSING_TOKEN",
                        "reconnect_attempts": 0,
                    }
                }
            }
        )
        output = runtime.connect("remote")
        self.assertIn("MCP_MISSING_TOKEN", output)
        self.assertIn("Failed to connect", output)

    def test_call_failure_is_not_replayed_and_next_call_reconnects(self):
        runtime = self.runtime(
            {
                "servers": {
                    "math": {
                        "command": "python",
                        "reconnect_attempts": 0,
                    }
                }
            },
            client_cls=FailingOnceClient,
        )
        runtime.connect("math")
        _, handlers = runtime.assemble_tool_pool([], [], {})
        first = handlers["mcp__math__add"](a=2, b=3)
        self.assertIn("not replayed", first)
        self.assertEqual(FailingOnceClient.call_count, 1)

        second = handlers["mcp__math__add"](a=2, b=3)
        self.assertIn("5", second)
        # The successful second call enters a fresh persistent Client context.
        self.assertEqual(FailingOnceClient.enter_count, 2)
        # FailingOnceClient delegates successful calls to FakeClient, whose
        # counter is separate; importantly the failed call itself ran only once.
        self.assertEqual(FailingOnceClient.call_count, 2)

    def test_explicit_connect_reconnects_an_invalidated_client(self):
        runtime = self.runtime(
            {"servers": {"math": {"command": "python", "reconnect_attempts": 0}}},
            client_cls=FailingOnceClient,
        )
        runtime.connect("math")
        _, handlers = runtime.assemble_tool_pool([], [], {})
        handlers["mcp__math__add"](a=1, b=1)
        output = runtime.connect("math")
        self.assertIn("Reconnected", output)
        self.assertIn("add", output)

    def test_close_closes_persistent_client_context(self):
        runtime = self.runtime({"servers": {"math": {"command": "python"}}})
        runtime.connect("math")
        runtime.close()
        self.assertEqual(FakeClient.exit_count, 1)
        self.assertFalse(runtime.clients["math"].is_connected)
        runtime.close()  # idempotent

    def test_workspace_server_script_still_requires_explicit_opt_in(self):
        (self.workspace / "server.py").write_text("# test", encoding="utf-8")
        runtime = self.runtime(
            {"servers": {"local": {"command": "python", "args": ["server.py"], "reconnect_attempts": 0}}}
        )
        output = runtime.connect("local")
        self.assertIn("allow_workspace_code=true", output)


    def test_paginated_tool_discovery_collects_every_page(self):
        class PaginatedClient(FakeClient):
            enter_count = 0
            exit_count = 0
            call_count = 0
            servers = []

            async def list_tools(self, cursor=None):
                if cursor is None:
                    return SimpleNamespace(
                        tools=[FakeTool("first")],
                        next_cursor="page-2",
                    )
                self.assert_cursor = cursor
                return SimpleNamespace(
                    tools=[FakeTool("second")],
                    next_cursor=None,
                )

        runtime = self.runtime(
            {"servers": {"math": {"command": "python"}}},
            client_cls=PaginatedClient,
        )
        output = runtime.connect("math")
        self.assertIn("first", output)
        self.assertIn("second", output)
        tools, _ = runtime.assemble_tool_pool([], [], {})
        self.assertEqual(
            {tool["name"] for tool in tools},
            {"mcp__math__first", "mcp__math__second"},
        )

    def test_tool_count_limit_fails_connection(self):
        runtime = self.runtime(
            {"servers": {"math": {"command": "python", "max_tools": 2, "reconnect_attempts": 0}}},
            client_cls=ManyToolsClient,
        )
        output = runtime.connect("math")
        self.assertIn("limit is 2", output)

    def test_normalized_tool_collision_fails_connection(self):
        runtime = self.runtime(
            {"servers": {"math": {"command": "python", "reconnect_attempts": 0}}},
            client_cls=CollisionToolsClient,
        )
        output = runtime.connect("math")
        self.assertIn("normalize to the same name", output)

    def test_mock_fallback_remains_available(self):
        fallback = MockMCPRuntime(
            arxiv_search=lambda query, **_: f"found:{query}",
            terminal_print=lambda *_: None,
        )
        runtime = self.runtime({"servers": {}}, fallback=fallback)
        self.assertIn("Connected to MCP server 'docs'", runtime.connect("docs"))
        tools, handlers = runtime.assemble_tool_pool([], [], {})
        self.assertIn("mcp__docs__search", {tool["name"] for tool in tools})
        self.assertEqual(handlers["mcp__docs__search"](query="pde"), "found:pde")


    def test_http_connection_is_reused_across_multiple_calls(self):
        runtime = self.runtime(
            {
                "servers": {
                    "remote": {
                        "transport": "streamable-http",
                        "url": "https://mcp.example.com/mcp",
                    }
                }
            }
        )
        runtime.connect("remote")
        _, handlers = runtime.assemble_tool_pool([], [], {})
        self.assertIn("2", handlers["mcp__remote__add"](a=1, b=1))
        self.assertIn("4", handlers["mcp__remote__add"](a=2, b=2))
        self.assertEqual(FakeClient.enter_count, 1)
        self.assertEqual(len(FakeHTTPClient.created), 1)
        self.assertEqual(len(HTTP_TRANSPORTS), 1)

    def test_stdio_parameters_preserve_explicit_env_and_cwd(self):
        runtime = self.runtime(
            {
                "servers": {
                    "math": {
                        "command": "python",
                        "args": ["--serve"],
                        "env": {"MODE": "test"},
                        "cwd": ".",
                    }
                }
            }
        )
        runtime.connect("math")
        params = FakeStdioServerParameters.created[-1]
        self.assertEqual(params.command, "python")
        self.assertEqual(params.args, ["--serve"])
        self.assertEqual(params.env, {"MODE": "test"})
        self.assertEqual(Path(params.cwd), self.workspace.resolve())
        self.assertEqual(params.encoding, "utf-8")

    def test_workspace_server_script_can_be_explicitly_trusted(self):
        (self.workspace / "server.py").write_text("# test", encoding="utf-8")
        runtime = self.runtime(
            {
                "servers": {
                    "local": {
                        "command": "python",
                        "args": ["server.py"],
                        "allow_workspace_code": True,
                    }
                }
            }
        )
        self.assertIn("Connected", runtime.connect("local"))

    def test_result_output_is_bounded(self):
        class LargeOutputClient(FakeClient):
            enter_count = 0
            exit_count = 0
            call_count = 0
            servers = []

            async def call_tool(self, name: str, arguments: dict):
                self.__class__.call_count += 1
                return SimpleNamespace(
                    content=[FakeText("x" * 500)],
                    structured_content=None,
                    is_error=False,
                )

        runtime = self.runtime(
            {
                "servers": {
                    "math": {
                        "command": "python",
                        "max_output_chars": 32,
                    }
                }
            },
            client_cls=LargeOutputClient,
        )
        runtime.connect("math")
        _, handlers = runtime.assemble_tool_pool([], [], {})
        output = handlers["mcp__math__add"](a=1, b=2)
        self.assertTrue(output.endswith("...<truncated>"))
        self.assertLess(len(output), 60)

    def test_normalized_server_name_collision_is_rejected(self):
        path = self.write_config(
            {
                "servers": {
                    "a.b": {"command": "python"},
                    "a_b": {"command": "python"},
                }
            }
        )
        with self.assertRaises(MCPConfigurationError):
            load_mcp_server_specs(path)

    def test_malformed_config_does_not_crash_runtime_constructor(self):
        path = self.trusted / "mcp_servers.json"
        path.write_text("{bad", encoding="utf-8")
        logs = []
        runtime = MCPRuntime(
            path,
            workspace=self.workspace,
            terminal_print=logs.append,
            sdk_loader=lambda: fake_sdk_loader(),
        )
        self.runtimes.append(runtime)
        self.assertIsNotNone(runtime.config_error)
        self.assertIn("configuration error", runtime.connect("math").lower())
        self.assertTrue(logs)

    def test_authorization_header_and_bearer_env_cannot_both_be_configured(self):
        path = self.write_config(
            {
                "servers": {
                    "remote": {
                        "transport": "streamable-http",
                        "url": "https://mcp.example.com/mcp",
                        "headers": {"Authorization": "Bearer static"},
                        "bearer_token_env": "MCP_TOKEN",
                    }
                }
            }
        )
        with self.assertRaises(MCPConfigurationError):
            load_mcp_server_specs(path)

    def test_http_url_must_not_embed_credentials(self):
        path = self.write_config(
            {
                "servers": {
                    "remote": {
                        "transport": "streamable-http",
                        "url": "https://user:pass@mcp.example.com/mcp",
                    }
                }
            }
        )
        with self.assertRaises(MCPConfigurationError):
            load_mcp_server_specs(path)

    def test_trusted_config_inside_workspace_is_rejected(self):
        path = self.workspace / "mcp_servers.json"
        path.write_text(json.dumps({"servers": {}}), encoding="utf-8")
        runtime = MCPRuntime(
            path,
            workspace=self.workspace,
            terminal_print=lambda *_: None,
            sdk_loader=lambda: fake_sdk_loader(),
        )
        self.runtimes.append(runtime)
        self.assertIn("outside the agent-writable workspace", runtime.config_error or "")


if __name__ == "__main__":
    unittest.main()
