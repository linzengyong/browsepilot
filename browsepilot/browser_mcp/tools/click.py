"""Click tool — click an element by reference label."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import ToolError, _get_selector_map


@mcp.tool()
async def click(element_ref: str, ctx: Context) -> dict:
    """点击页面元素。element_ref 必须从最近一次 get_page_structure 返回的元素列表中获取，禁止凭空编造。"""
    browser = ctx.request_context.lifespan_context["browser"]
    page = await browser.get_page()
    await browser.dismiss_dialogs()

    selector_map = _get_selector_map(ctx)
    selector = selector_map.get(element_ref)
    if not selector:
        # Fallback: treat element_ref as a raw CSS selector
        if element_ref and (element_ref.startswith("#") or element_ref.startswith(".") or element_ref.startswith("[")):
            selector = element_ref
        else:
            raise ToolError(
                error_type="element_not_found",
                message=f"元素引用 '{element_ref}' 未在映射表中找到，请先调用 get_page_structure",
                available_refs=list(selector_map.keys()),
            )

    try:
        await page.wait_for_selector(selector, timeout=5000)
    except Exception:
        raise ToolError(
            error_type="timeout",
            message=f"等待元素 '{element_ref}' 出现超时（5秒），页面可能仍在加载中",
            available_refs=list(selector_map.keys()),
        )

    url_before = page.url
    try:
        await page.click(selector)
    except Exception as e:
        raise ToolError(
            error_type="click_failed",
            message=f"点击元素 '{element_ref}' 失败: {e}",
            available_refs=list(selector_map.keys()),
        )

    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    url_after = page.url
    if url_after != url_before:
        try:
            await asyncio.wait_for(page.wait_for_load_state("domcontentloaded"), timeout=3)
        except Exception:
            pass

    screenshot = await browser.screenshot()
    from browser_mcp.tools import _save_screenshot
    path = _save_screenshot(screenshot)
    return {
        "status": "success",
        "screenshot_path": path,
        "url_before": url_before,
        "url_after": url_after,
    }
