from __future__ import annotations

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("researchagent-http-demo")


@mcp.tool()
def add(a: int, b: int) -> dict[str, int]:
    """Add two integers."""
    return {"result": a + b}


@mcp.tool()
def echo(text: str) -> str:
    """Echo text back to the caller."""
    return text


if __name__ == "__main__":
    # Local development endpoint: http://127.0.0.1:8000/mcp
    # stateless_http/json_response are the recommended simple HTTP-server mode
    # for a demo that does not need legacy protocol sessions.
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        json_response=True,
    )
