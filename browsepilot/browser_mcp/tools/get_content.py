"""Get content tool — extract page text or HTML."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp


@mcp.tool()
async def get_content(ctx: Context, format: str = "text") -> dict:
    """Extract page content as text or HTML."""
    browser = ctx.request_context.lifespan_context["browser"]
    page = await browser.get_page()
    try:
        if format == "html":
            content = await asyncio.wait_for(page.content(), timeout=15.0)
        else:
            content = await asyncio.wait_for(page.inner_text("body"), timeout=15.0)
        screenshot = await asyncio.wait_for(browser.screenshot(), timeout=15.0)
        return {"status": "success", "content": content[:10000], "screenshot_base64": screenshot}
    except asyncio.TimeoutError:
        return {"status": "error", "error": "timeout", "message": "get_content timed out"}
    except Exception as e:
        return {"status": "error", "error": str(e), "content": ""}
