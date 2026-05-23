"""Type text tool — type into an input element."""

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import ToolError, _get_selector_map, _save_screenshot


@mcp.tool()
async def type_text(element_ref: str, text: str, ctx: Context) -> dict:
    """向输入框输入文本。element_ref 必须从最近一次 get_page_structure 返回的输入框列表中获取。text 为要输入的纯文本内容，无需添加 Enter 或特殊字符。"""
    browser = ctx.request_context.lifespan_context["browser"]
    page = await browser.get_page()
    await browser.dismiss_dialogs()

    selector_map = _get_selector_map(ctx)
    available_refs = list(selector_map)
    selector = selector_map.get(element_ref)
    if not selector:
        if element_ref and (element_ref.startswith("#") or element_ref.startswith(".") or element_ref.startswith("[")):
            selector = element_ref
        else:
            raise ToolError(
                error_type="element_not_found",
                message=f"元素引用 '{element_ref}' 未在映射表中找到，请先调用 get_page_structure",
                available_refs=available_refs,
            )

    try:
        await page.wait_for_selector(selector, timeout=5000)
    except Exception:
        raise ToolError(
            error_type="timeout",
            message=f"等待输入框 '{element_ref}' 出现超时（5秒）",
            available_refs=available_refs,
        )

    try:
        await page.fill(selector, text)
    except Exception as e:
        raise ToolError(
            error_type="fill_failed",
            message=f"在 '{element_ref}' 中输入文本失败: {e}",
            available_refs=available_refs,
        )

    screenshot = await browser.screenshot()
    path = _save_screenshot(screenshot)
    return {"status": "success", "screenshot_path": path}
