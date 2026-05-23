"""Click tool — click an element by selector."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import _save_screenshot


@mcp.tool()
async def click(selector: str, ctx: Context) -> dict:
    """Click an element identified by a CSS selector."""
    browser = ctx.request_context.lifespan_context["browser"]
    page = await browser.get_page()
    await browser.dismiss_dialogs()
    try:
        await page.wait_for_selector(selector, timeout=5000)

        url_before = page.url

        await page.click(selector)

        # Wait for potential navigation triggered by the click
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        url_after = page.url

        # If URL changed, wait a bit more for dynamic content
        if url_after != url_before:
            try:
                await asyncio.wait_for(page.wait_for_load_state("domcontentloaded"), timeout=3)
            except Exception:
                pass

        screenshot = await browser.screenshot()
        return {
            "status": "success",
            "screenshot_path": _save_screenshot(screenshot),
            "url_before": url_before,
            "url_after": url_after,
        }
    except Exception as e:
        screenshot = await browser.screenshot()
        return {"status": "error", "error": "selector_not_found", "detail": str(e), "screenshot_path": _save_screenshot(screenshot)}
