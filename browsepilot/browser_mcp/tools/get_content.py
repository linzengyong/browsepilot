"""Get content tool — extract page text or HTML."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import ToolError, _save_screenshot


@mcp.tool()
async def get_content(ctx: Context, format: str = "text", max_chars: int = 10000) -> dict:
    """提取当前页面可见文本内容。建议先使用 scroll 滚动到目标区域后再调用。format 参数可选 "text"（默认）或 "html"。max_chars 控制最大返回字符数（默认 10000）。"""
    browser = ctx.request_context.lifespan_context["browser"]
    page = await browser.get_page()
    try:
        if format == "html":
            content = await asyncio.wait_for(page.content(), timeout=15.0)
        else:
            content = await asyncio.wait_for(page.inner_text("body"), timeout=15.0)
        screenshot = await asyncio.wait_for(browser.screenshot(), timeout=15.0)
        return {"status": "success", "content": content[:max_chars], "screenshot_path": _save_screenshot(screenshot)}
    except asyncio.TimeoutError:
        raise ToolError("timeout", "get_content timed out")
    except Exception as e:
        raise ToolError("content_extraction_failed", str(e))
