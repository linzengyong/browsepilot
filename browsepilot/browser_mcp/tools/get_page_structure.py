"""Get page structure tool — extract interactive elements with selectors."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import _save_screenshot


@mcp.tool()
async def get_page_structure(ctx: Context) -> dict:
    """获取当前页面所有可交互元素（输入框、按钮、链接）及对应的引用标签。在执行任何 click 或 type_text 前必须调用此工具，以获取有效的元素引用标签。"""
    browser = ctx.request_context.lifespan_context["browser"]
    page = await browser.get_page()
    try:
        structure = await asyncio.wait_for(page.evaluate("""
            () => {
                const getBestSelector = (el) => {
                    if (el.id) return '#' + CSS.escape(el.id);
                    if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
                    if (el.className && typeof el.className === 'string') {
                        const cls = el.className.trim().split(/\\s+/)[0];
                        if (cls) return el.tagName.toLowerCase() + '.' + CSS.escape(cls);
                    }
                    const placeholder = el.getAttribute('placeholder');
                    if (placeholder) return el.tagName.toLowerCase() + '[placeholder="' + placeholder + '"]';
                    const ariaLabel = el.getAttribute('aria-label');
                    if (ariaLabel) return el.tagName.toLowerCase() + '[aria-label="' + ariaLabel + '"]';
                    return el.tagName.toLowerCase();
                };

                const inputs = [];
                document.querySelectorAll('input, textarea, select, [contenteditable="true"]').forEach(el => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        inputs.push({
                            selector: getBestSelector(el),
                            tag: el.tagName.toLowerCase(),
                            type: el.type || '',
                            name: el.name || '',
                            id: el.id || '',
                            placeholder: el.getAttribute('placeholder') || '',
                            text: (el.value || el.textContent || '').slice(0, 50),
                        });
                    }
                });

                const buttons = [];
                document.querySelectorAll('button, input[type="submit"], input[type="button"], a[role="button"], [role="button"]').forEach(el => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        buttons.push({
                            selector: getBestSelector(el),
                            tag: el.tagName.toLowerCase(),
                            text: (el.textContent || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 60),
                            id: el.id || '',
                        });
                    }
                });

                const links = [];
                document.querySelectorAll('a[href]').forEach(el => {
                    const rect = el.getBoundingClientRect();
                    const text = (el.textContent || '').trim();
                    if (rect.width > 0 && rect.height > 0 && text.length > 0 && text.length < 100) {
                        links.push({
                            selector: getBestSelector(el),
                            text: text.slice(0, 60),
                            href: el.href.slice(0, 200),
                        });
                    }
                });

                return { inputs: inputs.slice(0, 20), buttons: buttons.slice(0, 20), links: links.slice(0, 30) };
            }
        """), timeout=15.0)
        screenshot = await asyncio.wait_for(browser.screenshot(), timeout=15.0)
        return {"status": "success", "structure": structure, "screenshot_path": _save_screenshot(screenshot)}
    except asyncio.TimeoutError:
        return {"status": "error", "error": "timeout", "message": "get_page_structure timed out"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
