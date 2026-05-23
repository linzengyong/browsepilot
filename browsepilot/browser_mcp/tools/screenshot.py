"""Screenshot tool — take a full page screenshot."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp


@mcp.tool()
async def screenshot(ctx: Context, full_page: bool = True) -> dict:
    """截取当前页面截图。仅当需要视觉信息辅助判断时使用，非必要不调用。"""
    browser = ctx.request_context.lifespan_context["browser"]
    try:
        data = await asyncio.wait_for(browser.screenshot(full_page=full_page), timeout=15.0)
        if not data:
            return {"status": "error", "error": "screenshot_failed", "screenshot_base64": ""}
        return {"status": "success", "screenshot_base64": data}
    except asyncio.TimeoutError:
        return {"status": "error", "error": "timeout", "message": "screenshot timed out"}
    except Exception as e:
        return {"status": "error", "error": str(e), "screenshot_base64": ""}
