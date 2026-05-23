"""Screenshot tool — take a full page screenshot."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import ToolError


@mcp.tool()
async def screenshot(ctx: Context, full_page: bool = True) -> dict:
    """截取当前页面截图。仅当需要视觉信息辅助判断时使用，非必要不调用。"""
    browser = ctx.request_context.lifespan_context["browser"]
    try:
        data = await asyncio.wait_for(browser.screenshot(full_page=full_page), timeout=15.0)
        if not data:
            raise ToolError("screenshot_failed", "截图结果为空")
        return {"status": "success", "screenshot_base64": data}
    except asyncio.TimeoutError:
        raise ToolError("timeout", "screenshot timed out")
    except Exception as e:
        raise ToolError("screenshot_failed", str(e))
