"""Minimal real stdio MCP server for the ResearchAgent 11A smoke test."""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("ResearchAgent Demo MCP")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def echo(text: str) -> str:
    """Return the supplied text unchanged."""
    return text


if __name__ == "__main__":
    mcp.run()
