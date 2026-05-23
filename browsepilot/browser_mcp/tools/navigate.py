"""Navigate tool — go to a URL."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import _save_screenshot, validate_url


@mcp.tool()
async def navigate(url: str, ctx: Context) -> dict:
    """导航到指定 URL。URL 必须包含完整协议（https://），或使用最近一次 get_page_structure 中链接的完整 href。"""
    browser = ctx.request_context.lifespan_context["browser"]
    is_valid, error = validate_url(url)
    if not is_valid:
        return {"status": "error", "error": error}
    page = await browser.get_page()
    try:
        await asyncio.wait_for(page.goto(url, wait_until="domcontentloaded"), timeout=15)
        title = await page.title()
        screenshot = await browser.screenshot()
        return {"status": "success", "screenshot_path": _save_screenshot(screenshot), "title": title}
    except asyncio.TimeoutError:
        screenshot = await browser.screenshot()
        return {"status": "error", "error": "timeout", "screenshot_path": _save_screenshot(screenshot)}
    except Exception as e:
        screenshot = await browser.screenshot()
        return {"status": "error", "error": str(e), "screenshot_path": _save_screenshot(screenshot)}
