"""Type text tool — type into an input element."""

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import ToolError, _save_screenshot


@mcp.tool()
async def type_text(selector: str, text: str, ctx: Context) -> dict:
    """向输入框输入文本。element_ref 必须从最近一次 get_page_structure 返回的输入框列表中获取。text 为要输入的纯文本内容，无需添加 Enter 或特殊字符。"""
    browser = ctx.request_context.lifespan_context["browser"]
    page = await browser.get_page()
    await browser.dismiss_dialogs()
    try:
        await page.wait_for_selector(selector, timeout=5000)
        await page.fill(selector, text)
        screenshot = await browser.screenshot()
        return {"status": "success", "screenshot_path": _save_screenshot(screenshot)}
    except Exception as e:
        screenshot = await browser.screenshot()
        raise ToolError("selector_not_found", f"输入框填充失败: {e}", available_refs=None)
