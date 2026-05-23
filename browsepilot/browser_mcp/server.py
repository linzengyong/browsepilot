"""browser-mcp FastMCP server — browser automation tools with session-scoped Playwright."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP

_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


@asynccontextmanager
async def browser_lifespan(server):
    """Acquire a browser instance from the shared pool for each MCP session."""
    import browser_mcp.browser_pool as bp_module
    pooled = await bp_module.pool.acquire()
    try:
        yield {"browser": pooled.browser_manager}
    finally:
        await bp_module.pool.release(pooled)


mcp = FastMCP(
    "browser-mcp",
    lifespan=browser_lifespan,
)
