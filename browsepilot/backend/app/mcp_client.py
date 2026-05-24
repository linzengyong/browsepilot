"""MCP client — thin wrapper around fastmcp.Client for agent use."""
import asyncio
from typing import Any
from loguru import logger
from fastmcp import Client


class MCPClient:
    """Thin wrapper around fastmcp.Client for agent compatibility.

    server_config must be a dict with at least 'url', e.g.
    {"type": "streamable-http", "url": "http://localhost:8100/mcp"}.
    Use config.get_mcp_server_config() to load from mcp_settings.json.
    """

    def __init__(self, server_config: dict):
        if not server_config or "url" not in server_config:
            raise ValueError("server_config must contain 'url'")
        self.server_config = server_config
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
        url = self.server_config["url"]
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
        """Call a tool and return structured result. meta is passed through to the MCP server."""
        if not self._client:
            raise RuntimeError("MCP client not connected")
        kwargs = {"timeout": timeout, "raise_on_error": False}
        if meta:
            kwargs["meta"] = meta
        result = await self._client.call_tool(tool_name, arguments, **kwargs)

        if result.is_error:
            return {"status": "error", "error_type": "tool_error", "message": str(result.content)}

        # Extract structured data from MCP content items
        data: dict = {}
        for item in result.content:
            item_type = getattr(item, "type", "text")
            if item_type == "text" and item.text:
                text = item.text
                if "```yaml" in text:
                    parts = text.split("```yaml", 1)
                    if len(parts) > 1:
                        yaml_part = parts[1].split("```", 1)[0]
                        # Only keep ref lines to avoid blowing up context
                        ref_lines = [l for l in yaml_part.split("\n") if "ref=e" in l]
                        data["snapshot"] = "\n".join(ref_lines[:30])
                        data["content"] = parts[0].strip()
                elif "```" in text:
                    data["content"] = (data.get("content", "") + "\n" + text).strip()
                else:
                    data["content"] = (data.get("content", "") + "\n" + text).strip()
            elif item_type == "image":
                img_data = getattr(item, "data", None)
                if img_data:
                    data["screenshot_base64"] = img_data
            elif item_type == "resource":
                resource_text = getattr(item, "resource", None)
                if resource_text:
                    data["content"] = (data.get("content", "") + "\n" + str(resource_text)).strip()

        return {"status": "success", **data}

    async def close(self) -> None:
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None
        logger.info("MCP client disconnected")
