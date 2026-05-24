"""browser-mcp server — Pool-managed Playwright MCP proxy + custom tools."""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP
from loguru import logger

_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

USE_PLAYWRIGHT_MCP = os.getenv("USE_PLAYWRIGHT_MCP", "false").lower() == "true"

if USE_PLAYWRIGHT_MCP:
    # When using Playwright MCP, browsepilot-mcp is just a thin wrapper.
    # The backend connects to the pool instance directly — no proxy needed.
    # This server exists only for backward compatibility (startup scripts).
    @asynccontextmanager
    async def _empty_lifespan(server):
        yield {}

    mcp = FastMCP("browsepilot-mcp", lifespan=_empty_lifespan)
else:
    @asynccontextmanager
    async def browser_lifespan(server):
        """Acquire a browser instance from the shared pool for each MCP session."""
        import browser_mcp.browser_pool as bp_module
        pooled = await bp_module.pool.acquire()
        element_map: dict[str, str] = {}
        try:
            yield {"browser": pooled.browser_manager, "element_map": element_map}
        finally:
            await bp_module.pool.release(pooled)

    mcp = FastMCP("browser-mcp", lifespan=browser_lifespan)
