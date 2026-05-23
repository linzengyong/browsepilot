"""MCP client — thin wrapper around fastmcp.Client for agent use.

Note: This wrapper explicitly calls __aenter__/__aexit__ (the protocol methods
underlying `async with Client(...) as c:`) rather than using the context manager
syntax. This is intentional — the agent code uses connect()/close() lifecycle
methods, and fastmcp.Client supports direct __aenter__/__aexit__ calls in v3.3.1.
"""

import asyncio
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

    async def connect(self, max_retries: int = 3) -> list[dict]:
        """Connect to MCP server via fastmcp.Client with retry."""
        url = self.server_config.get("url", "http://localhost:8090/mcp")
        last_error = None
        for attempt in range(max_retries):
            try:
                logger.info("Connecting to MCP server at {} (attempt {}/{})", url, attempt + 1, max_retries)
                self._client = Client(url)
                await self._client.__aenter__()
                self._tools = await self._client.list_tools()
                self._tools = [
                    {"name": t.name, "description": t.description or "", "parameters": getattr(t, "inputSchema", {})}
                    for t in self._tools
                ]
                logger.info("Discovered {} tools: {}", len(self._tools), [t["name"] for t in self._tools])
                return self._tools
            except Exception as e:
                last_error = e
                if self._client:
                    try:
                        await self._client.__aexit__(None, None, None)
                    except Exception:
                        pass
                    self._client = None
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning("MCP connection attempt {} failed, retrying in {}s: {}", attempt + 1, wait, e)
                    await asyncio.sleep(wait)
        raise RuntimeError(f"MCP connection failed after {max_retries} attempts: {last_error}")

    async def call_tool(self, tool_name: str, arguments: dict, timeout: float = 30.0, meta: dict | None = None) -> dict:
        """Call a tool and return structured result. meta is passed through to the MCP server for observability and context injection (session_id, step_index)."""
        if not self._client:
            raise RuntimeError("MCP client not connected")
        kwargs = {"timeout": timeout, "raise_on_error": False}
        if meta:
            kwargs["meta"] = meta
        result = await self._client.call_tool(tool_name, arguments, **kwargs)
        if result.is_error:
            data = result.data if isinstance(result.data, dict) else {}
            return {"status": "error", "error_type": "tool_error", "message": str(result.content), **data}
        data = result.data if isinstance(result.data, dict) else {}
        return {"status": "success", **data}

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
