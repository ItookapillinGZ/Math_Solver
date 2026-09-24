from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import os
import re
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse


class MCPConfigurationError(ValueError):
    """Raised when a configured MCP server is invalid."""


class MCPDependencyError(RuntimeError):
    """Raised when the optional official MCP SDK is unavailable."""


@dataclass(frozen=True)
class MCPSDKBindings:
    """Late-bound official SDK types used by the transport runtime.

    Keeping imports behind a loader leaves the base project importable when the
    optional MCP dependency is not installed and makes transport tests fully
    deterministic without starting real subprocesses or HTTP servers.
    """

    client_cls: type
    stdio_parameters_cls: type
    streamable_http_client: Callable[..., Any]
    async_http_client_cls: type
    http_timeout_cls: type


@dataclass(frozen=True)
class MCPServerSpec:
    """Human-owned configuration for one real MCP server."""

    name: str
    transport: str = "stdio"

    # stdio
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    allow_workspace_code: bool = False

    # Streamable HTTP
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    bearer_token_env: str | None = None
    allow_insecure_http: bool = False
    read_timeout_seconds: float = 300.0
    terminate_on_close: bool = True

    # Common lifecycle / safety limits
    timeout_seconds: float = 20.0
    reconnect_attempts: int = 2
    reconnect_backoff_seconds: float = 0.25
    shutdown_timeout_seconds: float = 5.0
    max_tools: int = 200
    max_output_chars: int = 50_000

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise MCPConfigurationError("MCP server name cannot be empty")
        object.__setattr__(self, "name", name)

        transport = str(self.transport).strip().lower().replace("_", "-")
        if transport in {"http", "streamablehttp"}:
            transport = "streamable-http"
        if transport not in {"stdio", "streamable-http"}:
            raise MCPConfigurationError(
                f"MCP server '{self.name}' uses unsupported transport '{self.transport}'"
            )
        object.__setattr__(self, "transport", transport)

        if self.timeout_seconds <= 0:
            raise MCPConfigurationError("timeout_seconds must be positive")
        if self.read_timeout_seconds <= 0:
            raise MCPConfigurationError("read_timeout_seconds must be positive")
        if self.reconnect_attempts < 0:
            raise MCPConfigurationError("reconnect_attempts cannot be negative")
        if self.reconnect_backoff_seconds < 0:
            raise MCPConfigurationError("reconnect_backoff_seconds cannot be negative")
        if self.shutdown_timeout_seconds <= 0:
            raise MCPConfigurationError("shutdown_timeout_seconds must be positive")
        if self.max_tools < 1:
            raise MCPConfigurationError("max_tools must be positive")
        if self.max_output_chars < 1:
            raise MCPConfigurationError("max_output_chars must be positive")

        object.__setattr__(self, "args", tuple(str(arg) for arg in self.args))
        object.__setattr__(
            self,
            "env",
            {str(key): str(value) for key, value in dict(self.env).items()},
        )
        headers = {str(key): str(value) for key, value in dict(self.headers).items()}
        for key, value in headers.items():
            if not key.strip() or any(ch in key for ch in "\r\n"):
                raise MCPConfigurationError(
                    f"MCP server '{self.name}' has an invalid HTTP header name"
                )
            if any(ch in value for ch in "\r\n"):
                raise MCPConfigurationError(
                    f"MCP server '{self.name}' has an invalid HTTP header value"
                )
        object.__setattr__(self, "headers", headers)

        if self.bearer_token_env is not None:
            env_name = str(self.bearer_token_env).strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
                raise MCPConfigurationError(
                    f"MCP server '{self.name}' bearer_token_env is not a valid environment variable name"
                )
            if any(key.casefold() == "authorization" for key in headers):
                raise MCPConfigurationError(
                    f"MCP server '{self.name}' cannot set both Authorization header and bearer_token_env"
                )
            object.__setattr__(self, "bearer_token_env", env_name)

        if transport == "stdio":
            object.__setattr__(self, "command", self.command.strip())
            if not self.command:
                raise MCPConfigurationError(
                    f"MCP server '{self.name}' requires a non-empty command"
                )
        else:
            object.__setattr__(self, "url", self.url.strip())
            self._validate_http_url()

    def _validate_http_url(self) -> None:
        raw = self.url.strip()
        if not raw:
            raise MCPConfigurationError(
                f"MCP server '{self.name}' requires a URL for streamable-http"
            )
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise MCPConfigurationError(
                f"MCP server '{self.name}' has an invalid Streamable HTTP URL"
            )
        if parsed.username is not None or parsed.password is not None:
            raise MCPConfigurationError(
                f"MCP server '{self.name}' URL must not contain embedded credentials"
            )
        if parsed.fragment:
            raise MCPConfigurationError(
                f"MCP server '{self.name}' URL must not contain a fragment"
            )
        if parsed.scheme == "http" and not (
            self.allow_insecure_http or _is_loopback_host(parsed.hostname)
        ):
            raise MCPConfigurationError(
                f"MCP server '{self.name}' uses insecure non-loopback HTTP; use HTTPS "
                "or set allow_insecure_http=true in trusted configuration"
            )

    @classmethod
    def from_mapping(cls, name: str, raw: Mapping[str, Any]) -> "MCPServerSpec":
        return cls(
            name=name,
            transport=str(raw.get("transport", "stdio")),
            command=str(raw.get("command", "")),
            args=tuple(raw.get("args", ()) or ()),
            env=dict(raw.get("env", {}) or {}),
            cwd=(str(raw["cwd"]) if raw.get("cwd") is not None else None),
            allow_workspace_code=bool(raw.get("allow_workspace_code", False)),
            url=str(raw.get("url", "")),
            headers=dict(raw.get("headers", {}) or {}),
            bearer_token_env=(
                str(raw["bearer_token_env"])
                if raw.get("bearer_token_env") is not None
                else None
            ),
            allow_insecure_http=bool(raw.get("allow_insecure_http", False)),
            read_timeout_seconds=float(raw.get("read_timeout_seconds", 300.0)),
            terminate_on_close=bool(raw.get("terminate_on_close", True)),
            timeout_seconds=float(raw.get("timeout_seconds", 20.0)),
            reconnect_attempts=int(raw.get("reconnect_attempts", 2)),
            reconnect_backoff_seconds=float(raw.get("reconnect_backoff_seconds", 0.25)),
            shutdown_timeout_seconds=float(raw.get("shutdown_timeout_seconds", 5.0)),
            max_tools=int(raw.get("max_tools", 200)),
            max_output_chars=int(raw.get("max_output_chars", 50_000)),
        )


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def load_mcp_server_specs(config_path: Path) -> dict[str, MCPServerSpec]:
    """Load trusted MCP server configuration without interpolation or execution."""

    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MCPConfigurationError(
            f"invalid JSON in {path.name}: line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(data, dict):
        raise MCPConfigurationError("MCP config root must be a JSON object")
    raw_servers = data.get("servers", {})
    if not isinstance(raw_servers, dict):
        raise MCPConfigurationError("MCP config 'servers' must be an object")

    specs: dict[str, MCPServerSpec] = {}
    normalized_names: dict[str, str] = {}
    for raw_name, raw_spec in raw_servers.items():
        name = str(raw_name).strip()
        if not isinstance(raw_spec, dict):
            raise MCPConfigurationError(
                f"MCP server '{name}' configuration must be an object"
            )
        spec = MCPServerSpec.from_mapping(name, raw_spec)
        normalized = MCPRuntime.normalize_name(name)
        previous = normalized_names.get(normalized)
        if previous is not None:
            raise MCPConfigurationError(
                f"MCP server names '{previous}' and '{name}' normalize to the same tool prefix"
            )
        normalized_names[normalized] = name
        specs[name] = spec
    return specs


def _default_sdk_loader() -> MCPSDKBindings:
    try:
        import httpx2
        from mcp import Client, StdioServerParameters
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:  # optional dependency; keep base runtime importable
        raise MCPDependencyError(
            'Official MCP Python SDK v2 is not installed. Run: '
            'python -m pip install -U "mcp[cli]"'
        ) from exc
    return MCPSDKBindings(
        client_cls=Client,
        stdio_parameters_cls=StdioServerParameters,
        streamable_http_client=streamable_http_client,
        async_http_client_cls=httpx2.AsyncClient,
        http_timeout_cls=httpx2.Timeout,
    )


class MCPClient:
    """Small in-process teaching stand-in retained for tests/demo fallback."""

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, Callable] = {}

    def register(self, tool_defs: list[dict], handlers: dict[str, Callable]) -> None:
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> str:
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return handler(**args)
        except Exception as exc:
            return f"MCP error: {exc}"


class PersistentMCPClient:
    """Own one long-lived official MCP ``Client`` context on a worker loop.

    The synchronous Agent runtime submits tool calls into a dedicated asyncio
    loop using ``run_coroutine_threadsafe``. A transport failure invalidates the
    connection; the failed tool call is never replayed automatically because
    the runtime cannot know whether a side-effecting request reached the server.
    The next call reconnects, rediscovers tools, and then executes once.
    """

    def __init__(
        self,
        spec: MCPServerSpec,
        *,
        config_root: Path,
        workspace: Path,
        sdk_loader: Callable[[], MCPSDKBindings] = _default_sdk_loader,
        terminal_print: Callable[[str], None] = print,
    ) -> None:
        self.name = spec.name
        self.spec = spec
        self.config_root = Path(config_root).resolve()
        self.workspace = Path(workspace).resolve()
        self._sdk_loader = sdk_loader
        self._terminal_print = terminal_print

        self.tools: list[dict] = []
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] | None = None

        self._state_lock = threading.RLock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any | None = None
        self._stop_event: asyncio.Event | None = None
        self._connected = False
        self._startup_error: BaseException | None = None
        self._runtime_error: BaseException | None = None

    @property
    def transport_label(self) -> str:
        return self.spec.transport

    @property
    def is_connected(self) -> bool:
        with self._state_lock:
            return bool(
                self._connected
                and self._thread is not None
                and self._thread.is_alive()
                and self._loop is not None
                and not self._loop.is_closed()
            )

    def discover(self) -> list[dict]:
        return self.connect()

    def connect(self) -> list[dict]:
        if self.is_connected:
            return copy.deepcopy(self.tools)

        last_error: BaseException | None = None
        attempts = self.spec.reconnect_attempts + 1
        for attempt in range(attempts):
            try:
                return self._connect_once()
            except BaseException as exc:
                last_error = exc
                self.close()
                if attempt + 1 < attempts:
                    delay = self.spec.reconnect_backoff_seconds * (2**attempt)
                    if delay > 0:
                        time.sleep(delay)
        assert last_error is not None
        raise last_error

    def _connect_once(self) -> list[dict]:
        with self._state_lock:
            if self.is_connected:
                return copy.deepcopy(self.tools)
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError(f"MCP server '{self.name}' connection is already starting")
            self._ready = threading.Event()
            self._startup_error = None
            self._runtime_error = None
            self._connected = False
            thread = threading.Thread(
                target=self._thread_main,
                name=f"mcp-{MCPRuntime.normalize_name(self.name)}",
                daemon=True,
            )
            self._thread = thread
            thread.start()

        if not self._ready.wait(timeout=self.spec.timeout_seconds):
            self.close()
            raise TimeoutError(
                f"MCP server '{self.name}' did not finish discovery within "
                f"{self.spec.timeout_seconds}s"
            )

        with self._state_lock:
            error = self._startup_error
            connected = self._connected
            tools = copy.deepcopy(self.tools)
        if error is not None:
            raise error
        if not connected:
            raise RuntimeError(
                f"MCP server '{self.name}' connection ended during startup"
            )
        return tools

    def call_tool(self, tool_name: str, args: dict) -> str:
        try:
            self.connect()
        except Exception as exc:
            return f"MCP error: failed to connect '{self.name}': {type(exc).__name__}: {exc}"

        if not any(tool.get("name") == tool_name for tool in self.tools):
            return f"MCP error: unknown tool '{tool_name}'"

        with self._state_lock:
            loop = self._loop
            client = self._client
        if loop is None or client is None or not self.is_connected:
            return f"MCP error: server '{self.name}' is not connected"

        future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(client, tool_name, dict(args or {})),
            loop,
        )
        try:
            return future.result(timeout=self.spec.timeout_seconds + 1.0)
        except FutureTimeoutError:
            future.cancel()
            self._invalidate_connection()
            return (
                f"MCP error: TimeoutError: tool '{tool_name}' exceeded "
                f"{self.spec.timeout_seconds}s. Connection reset; the tool call was not replayed."
            )
        except Exception as exc:
            self._invalidate_connection()
            return (
                f"MCP error: {type(exc).__name__}: {exc}. "
                "Connection reset; the tool call was not replayed."
            )

    async def _call_tool_async(self, client: Any, tool_name: str, args: dict) -> str:
        result = await asyncio.wait_for(
            client.call_tool(tool_name, args),
            timeout=self.spec.timeout_seconds,
        )
        return self._format_tool_result(result)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._state_lock:
            self._loop = loop
        try:
            loop.run_until_complete(self._session_main())
        except BaseException as exc:
            with self._state_lock:
                if not self._ready.is_set():
                    self._startup_error = exc
                else:
                    self._runtime_error = exc
                self._connected = False
            self._ready.set()
        finally:
            with self._state_lock:
                self._connected = False
                self._client = None
                self._stop_event = None
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                loop.close()
                with self._state_lock:
                    if self._loop is loop:
                        self._loop = None

    async def _session_main(self) -> None:
        bindings = self._sdk_loader()
        async with AsyncExitStack() as stack:
            client = await self._enter_client(stack, bindings)
            tools = await self._list_all_tools(client)
            self._validate_normalized_tool_names(tools)

            stop_event = asyncio.Event()
            with self._state_lock:
                self.tools = tools
                protocol = getattr(client, "protocol_version", None)
                self.protocol_version = str(protocol) if protocol else None
                self.server_info = self._serialize_server_info(
                    getattr(client, "server_info", None)
                )
                self._client = client
                self._stop_event = stop_event
                self._connected = True
            self._ready.set()
            await stop_event.wait()


    async def _list_all_tools(self, client: Any) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            result = await asyncio.wait_for(
                client.list_tools(cursor=cursor),
                timeout=self.spec.timeout_seconds,
            )
            tools.extend(self._normalize_tool(tool) for tool in result.tools)
            if len(tools) > self.spec.max_tools:
                raise RuntimeError(
                    f"MCP server '{self.name}' exposed more than {self.spec.max_tools} tools; limit is {self.spec.max_tools}"
                )
            next_cursor = getattr(result, "next_cursor", None)
            if next_cursor is None:
                return tools
            next_cursor = str(next_cursor)
            if next_cursor in seen_cursors:
                raise RuntimeError(
                    f"MCP server '{self.name}' repeated tools/list cursor {next_cursor!r}"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def _enter_client(
        self,
        stack: AsyncExitStack,
        bindings: MCPSDKBindings,
    ) -> Any:
        if self.spec.transport == "stdio":
            params = self._stdio_server_parameters(bindings.stdio_parameters_cls)
            return await asyncio.wait_for(
                stack.enter_async_context(bindings.client_cls(params)),
                timeout=self.spec.timeout_seconds,
            )

        headers = self._http_headers()
        timeout = bindings.http_timeout_cls(
            self.spec.timeout_seconds,
            read=self.spec.read_timeout_seconds,
        )
        http_client = await asyncio.wait_for(
            stack.enter_async_context(
                bindings.async_http_client_cls(headers=headers, timeout=timeout)
            ),
            timeout=self.spec.timeout_seconds,
        )
        transport = bindings.streamable_http_client(
            self.spec.url,
            http_client=http_client,
            terminate_on_close=self.spec.terminate_on_close,
        )
        return await asyncio.wait_for(
            stack.enter_async_context(bindings.client_cls(transport)),
            timeout=self.spec.timeout_seconds,
        )

    def _http_headers(self) -> dict[str, str]:
        headers = dict(self.spec.headers)
        if self.spec.bearer_token_env:
            token = os.getenv(self.spec.bearer_token_env)
            if not token:
                raise MCPConfigurationError(
                    f"MCP server '{self.name}' requires environment variable "
                    f"{self.spec.bearer_token_env} for bearer authentication"
                )
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _stdio_server_parameters(self, cls: type) -> Any:
        cwd = self.workspace
        if self.spec.cwd:
            candidate = (self.workspace / self.spec.cwd).resolve()
            if not candidate.is_relative_to(self.workspace):
                raise MCPConfigurationError(
                    f"MCP server '{self.name}' cwd escapes agent workspace"
                )
            cwd = candidate
        if not cwd.exists() or not cwd.is_dir():
            raise MCPConfigurationError(
                f"MCP server '{self.name}' cwd does not exist: {cwd}"
            )

        command = self.spec.command
        if any(separator in command for separator in ("/", "\\")):
            command_path = Path(command).expanduser()
            if not command_path.is_absolute():
                command_path = (self.config_root / command_path).resolve()
            else:
                command_path = command_path.resolve()
            if (
                command_path.is_relative_to(self.workspace)
                and not self.spec.allow_workspace_code
            ):
                raise MCPConfigurationError(
                    f"MCP server '{self.name}' executable is inside the agent-writable "
                    "workspace; set allow_workspace_code=true only for an explicitly "
                    "trusted local development server"
                )
            command = str(command_path)

        self._validate_workspace_code_args(list(self.spec.args), cwd)

        return cls(
            command=command,
            args=list(self.spec.args),
            env=dict(self.spec.env) or None,
            cwd=str(cwd),
            encoding="utf-8",
            encoding_error_handler="replace",
        )

    def _validate_workspace_code_args(self, args: list[str], cwd: Path) -> None:
        if self.spec.allow_workspace_code:
            return
        script_suffixes = {".py", ".js", ".mjs", ".cjs", ".ps1", ".bat", ".cmd"}
        for arg in args:
            if not isinstance(arg, str) or not arg or arg.startswith("-"):
                continue
            candidate = Path(arg)
            if candidate.suffix.lower() not in script_suffixes:
                continue
            if not candidate.is_absolute():
                candidate = (cwd / candidate).resolve()
            else:
                candidate = candidate.resolve()
            if candidate.is_relative_to(self.workspace):
                raise MCPConfigurationError(
                    f"MCP server '{self.name}' would execute workspace code '{arg}'; "
                    "set allow_workspace_code=true only for an explicitly trusted "
                    "local development server"
                )

    def _invalidate_connection(self) -> None:
        self.close()

    def close(self) -> None:
        with self._state_lock:
            loop = self._loop
            stop_event = self._stop_event
            thread = self._thread

        if loop is not None and stop_event is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass

        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=self.spec.shutdown_timeout_seconds)

        with self._state_lock:
            if thread is not None and not thread.is_alive() and self._thread is thread:
                self._thread = None
            self._connected = False
            self._client = None

    @staticmethod
    def _serialize_server_info(server_info: Any) -> dict[str, Any] | None:
        if server_info is None:
            return None
        if hasattr(server_info, "model_dump"):
            return server_info.model_dump(mode="json", by_alias=True)
        data = {
            key: getattr(server_info, key)
            for key in ("name", "version")
            if getattr(server_info, key, None) is not None
        }
        return data or None

    @staticmethod
    def _normalize_tool(tool: Any) -> dict:
        if hasattr(tool, "model_dump"):
            raw = tool.model_dump(mode="json", by_alias=True)
        elif isinstance(tool, dict):
            raw = dict(tool)
        else:
            raw = {
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", ""),
                "inputSchema": getattr(tool, "input_schema", None),
            }

        name = str(raw.get("name") or getattr(tool, "name", "")).strip()
        if not name:
            raise RuntimeError("MCP server returned a tool without a name")
        description = raw.get("description") or getattr(tool, "description", "") or ""
        schema = raw.get("inputSchema")
        if schema is None:
            schema = raw.get("input_schema")
        if schema is None:
            schema = getattr(tool, "input_schema", None)
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        return {
            "name": name,
            "description": str(description),
            "inputSchema": copy.deepcopy(schema),
        }

    @classmethod
    def _validate_normalized_tool_names(cls, tools: list[dict]) -> None:
        seen: dict[str, str] = {}
        for tool in tools:
            normalized = MCPRuntime.normalize_name(tool["name"])
            previous = seen.get(normalized)
            if previous is not None:
                raise RuntimeError(
                    f"MCP tools '{previous}' and '{tool['name']}' normalize to the same name"
                )
            seen[normalized] = tool["name"]

    def _format_tool_result(self, result: Any) -> str:
        parts: list[str] = []
        for block in list(getattr(result, "content", []) or []):
            block_type = getattr(block, "type", None)
            if block_type == "text" and getattr(block, "text", None) is not None:
                parts.append(str(block.text))
            elif block_type in {"image", "audio"}:
                mime_type = getattr(block, "mime_type", None) or "unknown"
                parts.append(f"[MCP {block_type} content omitted; mime_type={mime_type}]")
            elif block_type in {"resource", "resource_link"}:
                parts.append(f"[MCP {block_type} content returned]")
            else:
                parts.append(f"[MCP content block: {block_type or type(block).__name__}]")

        structured = getattr(result, "structured_content", None)
        if structured is not None:
            parts.append(
                "[Structured Content]\n"
                + json.dumps(structured, ensure_ascii=False, sort_keys=True)
            )
        text = "\n".join(part for part in parts if part).strip() or "(no MCP output)"
        if bool(getattr(result, "is_error", False)):
            text = "MCP tool reported an error.\n" + text
        if len(text) > self.spec.max_output_chars:
            text = text[: self.spec.max_output_chars] + "...<truncated>"
        return text


# Backward-compatible symbol from Step 11A. It now uses the persistent client
# implementation but is expected to be constructed with a stdio spec.
StdioMCPClient = PersistentMCPClient


class MockMCPRuntime:
    """In-process MCP teaching mock retained as an explicit fallback."""

    _DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

    def __init__(
        self,
        arxiv_search: Callable[..., str],
        terminal_print: Callable[[str], None] = print,
    ):
        self.arxiv_search = arxiv_search
        self.terminal_print = terminal_print
        self.clients: dict[str, MCPClient] = {}
        self._server_factories = {
            "docs": self._mock_server_docs,
            "deploy": self._mock_server_deploy,
        }

    @classmethod
    def normalize_name(cls, name: str) -> str:
        return cls._DISALLOWED_CHARS.sub("_", name)

    def _mock_server_docs(self) -> MCPClient:
        client = MCPClient("docs")
        client.register(
            tool_defs=[
                {
                    "name": "search",
                    "description": (
                        "Search academic papers from arXiv (e.g., reaction-diffusion equations, "
                        "PDE methods). Returns titles and summaries. (readOnly)"
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
                {
                    "name": "get_version",
                    "description": "Get API version. (readOnly)",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            ],
            handlers={
                "search": self.arxiv_search,
                "get_version": lambda: "[docs] API v2.1.0",
            },
        )
        return client

    @staticmethod
    def _mock_server_deploy() -> MCPClient:
        client = MCPClient("deploy")
        client.register(
            tool_defs=[
                {
                    "name": "trigger",
                    "description": (
                        "Trigger a deployment. (destructive — requires approval in real CC)"
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {"service": {"type": "string"}},
                        "required": ["service"],
                    },
                },
                {
                    "name": "status",
                    "description": "Check deployment status. (readOnly)",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"service": {"type": "string"}},
                        "required": ["service"],
                    },
                },
            ],
            handlers={
                "trigger": lambda service: f"[deploy] Triggered: {service}",
                "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
            },
        )
        return client

    def connect(self, name: str) -> str:
        if name in self.clients:
            return f"MCP server '{name}' already connected"
        factory = self._server_factories.get(name)
        if not factory:
            available = ", ".join(self._server_factories.keys())
            return f"Unknown server '{name}'. Available: {available}"
        client = factory()
        self.clients[name] = client
        tool_names = [tool["name"] for tool in client.tools]
        self.terminal_print(
            f"  \033[31m[mcp] connected: {name} → {tool_names}\033[0m"
        )
        return (
            f"Connected to MCP server '{name}'. "
            f"Discovered {len(client.tools)} tools: {', '.join(tool_names)}"
        )

    def list_servers(self) -> list[str]:
        return list(self.clients.keys())

    def assemble_tool_pool(
        self,
        registry_tools: list[dict],
        builtin_tools: list[dict],
        registry_handlers: dict[str, Callable],
    ) -> tuple[list[dict], dict[str, Callable]]:
        return _assemble_tool_pool(
            self.clients,
            registry_tools,
            builtin_tools,
            registry_handlers,
        )


class MCPRuntime:
    """Real stdio/Streamable-HTTP MCP runtime with explicit mock fallback."""

    _DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

    def __init__(
        self,
        config_path: Path,
        *,
        workspace: Path,
        terminal_print: Callable[[str], None] = print,
        fallback_runtime: MockMCPRuntime | None = None,
        sdk_loader: Callable[[], MCPSDKBindings] = _default_sdk_loader,
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.config_root = self.config_path.parent.resolve()
        self.workspace = Path(workspace).resolve()
        self.terminal_print = terminal_print
        self.fallback_runtime = fallback_runtime
        self.sdk_loader = sdk_loader
        self.clients: dict[str, MCPClient | PersistentMCPClient] = {}
        self.config_error: str | None = None
        try:
            if self.config_path.is_relative_to(self.workspace):
                raise MCPConfigurationError(
                    "trusted MCP config must live outside the agent-writable workspace"
                )
            self.specs = load_mcp_server_specs(self.config_path)
        except MCPConfigurationError as exc:
            self.specs = {}
            self.config_error = str(exc)
            self.terminal_print(f"  [mcp warning] {exc}")

    @classmethod
    def normalize_name(cls, name: str) -> str:
        return cls._DISALLOWED_CHARS.sub("_", str(name))

    def available_servers(self) -> list[str]:
        names = set(self.specs)
        if self.fallback_runtime is not None:
            names.update(self.fallback_runtime._server_factories)
        return sorted(names)

    def connect(self, name: str) -> str:
        name = str(name).strip()
        existing = self.clients.get(name)
        if isinstance(existing, PersistentMCPClient):
            if existing.is_connected:
                return f"MCP server '{name}' already connected"
            try:
                existing.connect()
            except Exception as exc:
                return f"Failed to reconnect MCP server '{name}': {type(exc).__name__}: {exc}"
            return self._connected_message(existing, reconnected=True)
        if existing is not None:
            return f"MCP server '{name}' already connected"
        if self.config_error:
            return f"MCP configuration error: {self.config_error}"

        spec = self.specs.get(name)
        if spec is not None:
            client = PersistentMCPClient(
                spec,
                config_root=self.config_root,
                workspace=self.workspace,
                sdk_loader=self.sdk_loader,
                terminal_print=self.terminal_print,
            )
            try:
                client.connect()
            except Exception as exc:
                client.close()
                return f"Failed to connect MCP server '{name}': {type(exc).__name__}: {exc}"
            self.clients[name] = client
            return self._connected_message(client, reconnected=False)

        if self.fallback_runtime is not None:
            result = self.fallback_runtime.connect(name)
            client = self.fallback_runtime.clients.get(name)
            if client is not None:
                self.clients[name] = client
            return result

        available = ", ".join(self.available_servers()) or "(none configured)"
        return f"Unknown server '{name}'. Available: {available}"

    def _connected_message(
        self,
        client: PersistentMCPClient,
        *,
        reconnected: bool,
    ) -> str:
        tool_names = [tool["name"] for tool in client.tools]
        protocol = f" protocol={client.protocol_version}" if client.protocol_version else ""
        action = "reconnected" if reconnected else "connected"
        self.terminal_print(
            f"  \033[31m[mcp] {action} real {client.transport_label}: "
            f"{client.name} → {tool_names}\033[0m"
        )
        verb = "Reconnected" if reconnected else "Connected"
        return (
            f"{verb} to MCP server '{client.name}' via {client.transport_label}.{protocol} "
            f"Discovered {len(tool_names)} tools: {', '.join(tool_names)}"
        )

    def close(self) -> None:
        for client in list(self.clients.values()):
            if isinstance(client, PersistentMCPClient):
                client.close()

    def list_servers(self) -> list[str]:
        return list(self.clients.keys())

    def assemble_tool_pool(
        self,
        registry_tools: list[dict],
        builtin_tools: list[dict],
        registry_handlers: dict[str, Callable],
    ) -> tuple[list[dict], dict[str, Callable]]:
        return _assemble_tool_pool(
            self.clients,
            registry_tools,
            builtin_tools,
            registry_handlers,
        )


def _assemble_tool_pool(
    clients: Mapping[str, Any],
    registry_tools: list[dict],
    builtin_tools: list[dict],
    registry_handlers: dict[str, Callable],
) -> tuple[list[dict], dict[str, Callable]]:
    tools = list(registry_tools) + list(builtin_tools)
    handlers = dict(registry_handlers)
    emitted_names = {str(tool.get("name", "")) for tool in tools}

    for server_name, client in clients.items():
        safe_server = MCPRuntime.normalize_name(server_name)
        for tool_def in client.tools:
            safe_tool = MCPRuntime.normalize_name(tool_def["name"])
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            if prefixed in emitted_names:
                raise RuntimeError(f"duplicate MCP tool name after normalization: {prefixed}")
            emitted_names.add(prefixed)
            tools.append(
                {
                    "name": prefixed,
                    "description": tool_def.get("description", ""),
                    "input_schema": copy.deepcopy(tool_def.get("inputSchema", {})),
                }
            )
            handlers[prefixed] = (
                lambda *, c=client, t=tool_def["name"], **kwargs: c.call_tool(t, kwargs)
            )
    return tools, handlers
