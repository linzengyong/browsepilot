"""Execute script tool — run limited safe JavaScript."""

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import filter_js_script


@mcp.tool()
async def execute_script(script: str, ctx: Context) -> dict:
    """Execute a safe JavaScript script on the page."""
    browser = ctx.request_context.lifespan_context["browser"]
    is_safe, error = filter_js_script(script)
    if not is_safe:
        return {"status": "error", "error": error, "result": None}
    page = await browser.get_page()
    try:
        result = await page.evaluate(script)
        return {"status": "success", "result": str(result)}
    except Exception as e:
        return {"status": "error", "error": str(e), "result": None}
