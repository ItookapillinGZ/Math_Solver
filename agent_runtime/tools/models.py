from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., str]
    source: str = "builtin"
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        # Normalize caller-provided sets/tuples into an immutable representation.
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))

    def to_api_schema(self) -> dict[str, Any]:
        # Capabilities are runtime metadata and are intentionally not exposed
        # as part of the Anthropic tool schema.
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
