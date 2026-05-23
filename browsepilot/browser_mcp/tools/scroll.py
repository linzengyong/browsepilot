"""Scroll tool — scroll the page up or down."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import ToolError, _save_screenshot, _get_screenshot_meta


@mcp.tool()
async def scroll(ctx: Context, direction: str = "down", amount: int = 500) -> dict:
    """滚动页面。默认向下滚动 500 像素。搜索结果页面请先滚动跳过顶部广告区域，再使用 get_content 获取有效信息。"""
    browser = ctx.request_context.lifespan_context["browser"]
    page = await browser.get_page()
    try:
        pixels = amount if direction == "down" else -amount
        await asyncio.wait_for(page.evaluate(f"window.scrollBy(0, {pixels})"), timeout=15.0)
        screenshot_data = await asyncio.wait_for(browser.screenshot(), timeout=15.0)
        session_id, step_index = _get_screenshot_meta(ctx)
        return {"status": "success", "screenshot_path": _save_screenshot(screenshot_data, session_id=session_id, step_index=step_index)}
    except asyncio.TimeoutError:
        raise ToolError("timeout", "scroll timed out")
    except Exception as e:
        raise ToolError("scroll_failed", str(e))
