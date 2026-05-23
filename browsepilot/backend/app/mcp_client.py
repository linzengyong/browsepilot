"""MCP client — thin wrapper around fastmcp.Client for agent use.

Note: This wrapper explicitly calls __aenter__/__aexit__ (the protocol methods
underlying `async with Client(...) as c:`) rather than using the context manager
syntax. This is intentional — the agent code uses connect()/close() lifecycle
methods, and fastmcp.Client supports direct __aenter__/__aexit__ calls in v3.3.1.
"""

from typing import Any
from loguru import logger
from fastmcp import Client


class MCPClient:
    """Thin wrapper around fastmcp.Client for agent compatibility."""

    def __init__(self, server_config: dict | None = None):
        self.server_config = server_config or {
            "type": "streamable-http",
            "url": "http://localhost:8090/mcp",
        }
        self._client: Client | None = None
        self._tools: list[dict] = []

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def tools(self) -> list[dict]:
        return self._tools

    async def connect(self) -> list[dict]:
        """Connect to MCP server via fastmcp.Client."""
        url = self.server_config.get("url", "http://localhost:8090/mcp")
        logger.info("Connecting to MCP server at {}", url)
        self._client = Client(url)
        await self._client.__aenter__()
        self._tools = await self._client.list_tools()
        self._tools = [
            {"name": t.name, "description": t.description or "", "parameters": getattr(t, "inputSchema", {})}
            for t in self._tools
        ]
        logger.info("Discovered {} tools: {}", len(self._tools), [t["name"] for t in self._tools])
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict, timeout: float = 30.0) -> dict:
        """Call a tool and return structured result."""
        if not self._client:
            raise RuntimeError("MCP client not connected")
        result = await self._client.call_tool(tool_name, arguments, timeout=timeout, raise_on_error=False)
        if result.is_error:
            return {"status": "error", "error_type": "tool_error", "message": str(result.content), **result.data}
        return {"status": "success", **result.data} if isinstance(result.data, dict) else {"status": "success", "result": result.data}

    async def get_tools_schema(self) -> list[dict]:
        """Return tools in OpenAI function-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"] if t["parameters"] else {"type": "object", "properties": {}},
                },
            }
            for t in self._tools
        ]

    async def close(self) -> None:
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None
        logger.info("MCP client disconnected")
