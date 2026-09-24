from typing import Any, Callable

from .models import ToolDefinition


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def get_handler(self, name: str):
        tool = self.get(name)
        return tool.handler if tool else None

    def capabilities_for(self, name: str) -> frozenset[str]:
        tool = self.get(name)
        return tool.capabilities if tool else frozenset()

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_api_schema() for tool in self._tools.values()]

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {name: tool.handler for name, tool in self._tools.items()}

    def names(self) -> list[str]:
        return list(self._tools.keys())
