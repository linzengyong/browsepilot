"""Scroll tool — scroll the page up or down."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import _save_screenshot


@mcp.tool()
async def scroll(ctx: Context, direction: str = "down", amount: int = 500) -> dict:
    """Scroll the page up or down by a given pixel amount."""
    browser = ctx.request_context.lifespan_context["browser"]
    page = await browser.get_page()
    try:
        pixels = amount if direction == "down" else -amount
        await asyncio.wait_for(page.evaluate(f"window.scrollBy(0, {pixels})"), timeout=15.0)
        screenshot_data = await asyncio.wait_for(browser.screenshot(), timeout=15.0)
        return {"status": "success", "screenshot_path": _save_screenshot(screenshot_data)}
    except asyncio.TimeoutError:
        return {"status": "error", "error": "timeout", "message": "scroll timed out"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
