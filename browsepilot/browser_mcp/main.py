"""browsepilot-mcp entry point — load config, register tools, start server."""
import os
import asyncio
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

from browser_mcp.tools import set_allowed_domains

domains = os.getenv("ALLOWED_DOMAINS", "")
if domains:
    set_allowed_domains([d.strip() for d in domains.split(",")])

USE_PLAYWRIGHT_MCP = os.getenv("USE_PLAYWRIGHT_MCP", "false").lower() == "true"

if USE_PLAYWRIGHT_MCP:
    from browser_mcp.playwright_mcp_pool import PlaywrightMCPPool
    import browser_mcp.playwright_mcp_pool as pool_module
    from loguru import logger

    pool = pool_module.pool = PlaywrightMCPPool(
        max_size=int(os.getenv("MCP_POOL_SIZE", "8")),
        prewarm=int(os.getenv("MCP_POOL_PREWARM", "1")),
        max_age_minutes=int(os.getenv("MCP_MAX_AGE_MINUTES", "30")),
        max_requests=int(os.getenv("MCP_MAX_REQUESTS", "50")),
        idle_timeout_minutes=int(os.getenv("MCP_IDLE_TIMEOUT", "10")),
        acquire_timeout=float(os.getenv("MCP_ACQUIRE_TIMEOUT", "30")),
        headless=os.getenv("BROWSER_HEADLESS", "true").lower() == "true",
    )

    async def main():
        await pool.start()
        logger.info("Playwright MCP pool started, keeping alive...")
        # Keep running forever — the pool manages its own lifecycle
        while True:
            await asyncio.sleep(3600)

    if __name__ == "__main__":
        asyncio.run(main())
else:
    import browser_mcp.tools.navigate           # noqa: F401
    import browser_mcp.tools.click              # noqa: F401
    import browser_mcp.tools.type_text          # noqa: F401
    import browser_mcp.tools.get_content        # noqa: F401
    import browser_mcp.tools.screenshot         # noqa: F401
    import browser_mcp.tools.scroll             # noqa: F401
    import browser_mcp.tools.execute_script     # noqa: F401
    import browser_mcp.tools.get_page_structure # noqa: F401

    from browser_mcp.server import mcp
    from browser_mcp.browser_pool import BrowserPool
    import browser_mcp.browser_pool as bp_module

    pool = bp_module.pool = BrowserPool(
        max_size=int(os.getenv("BROWSER_POOL_SIZE", "8")),
        prewarm=int(os.getenv("BROWSER_POOL_PREWARM", "2")),
        max_age_minutes=int(os.getenv("BROWSER_MAX_AGE_MINUTES", "30")),
        max_requests=int(os.getenv("BROWSER_MAX_REQUESTS", "50")),
        idle_timeout_minutes=int(os.getenv("BROWSER_IDLE_TIMEOUT", "10")),
        acquire_timeout=float(os.getenv("BROWSER_ACQUIRE_TIMEOUT", "30")),
        headless=os.getenv("BROWSER_HEADLESS", "true").lower() == "true",
        channel=os.getenv("BROWSER_CHANNEL", "") or None,
        browser_timeout=int(os.getenv("BROWSER_TIMEOUT", "15000")),
    )

    if __name__ == "__main__":
        mcp.run(
            transport="streamable-http",
            host="127.0.0.1",
            port=int(os.getenv("MCP_SERVER_PORT", "8091")),
        )
