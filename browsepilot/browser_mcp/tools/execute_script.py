"""Execute script tool — run limited safe JavaScript."""

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import filter_js_script


@mcp.tool()
async def execute_script(script: str, ctx: Context) -> dict:
    """在页面执行安全的 JavaScript 脚本。仅在其它 7 个工具均无法完成任务时作为最后手段使用。脚本会受到安全过滤。"""
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
