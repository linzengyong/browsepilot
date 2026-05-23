"""Get page structure tool — extract interactive elements with reference labels."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import ToolError


@mcp.tool()
async def get_page_structure(ctx: Context) -> dict:
    """获取当前页面所有可交互元素（输入框、按钮、链接）及对应的引用标签。在执行任何 click 或 type_text 前必须调用此工具，以获取有效的元素引用标签。"""
    browser = ctx.request_context.lifespan_context["browser"]
    element_map = ctx.request_context.lifespan_context.get("element_map", {})
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

                const getSemanticName = (el, tagType) => {
                    const placeholder = el.getAttribute('placeholder') || '';
                    const text = (el.textContent || el.value || '').trim().slice(0, 20);
                    const name = el.name || '';
                    const ariaLabel = el.getAttribute('aria-label') || '';
                    const id = el.id || '';
                    const raw = placeholder || text || name || ariaLabel || id || tagType;
                    return raw.replace(/[\\[\\]\\n\\r\\t]/g, '').slice(0, 20);
                };

                const inputs = [];
                document.querySelectorAll('input, textarea, select, [contenteditable="true"]').forEach(el => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        inputs.push({
                            selector: getBestSelector(el),
                            tag: el.tagName.toLowerCase(),
                            type: el.type || '',
                            placeholder: el.getAttribute('placeholder') || '',
                            text: (el.value || el.textContent || '').slice(0, 50),
                            semantic: getSemanticName(el, 'input'),
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
                            semantic: getSemanticName(el, 'btn'),
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
                            semantic: getSemanticName(el, 'link'),
                        });
                    }
                });

                return { inputs: inputs.slice(0, 20), buttons: buttons.slice(0, 20), links: links.slice(0, 30) };
            }
        """), timeout=15.0)

        # Build element map with labels
        elements: dict[str, dict] = {}
        name_counter: dict[str, int] = {}

        def add_element(tag_type: str, item: dict) -> None:
            base_name = item.get("semantic", tag_type) or tag_type
            if not base_name:
                base_name = tag_type
            label = f"[{tag_type}:{base_name}]"
            if label in name_counter:
                name_counter[label] += 1
                label = f"[{tag_type}:{base_name}_{name_counter[label]}]"
            else:
                name_counter[label] = 0
            elements[label] = {
                "tag": item.get("tag", ""),
                "type": item.get("type", ""),
                "placeholder": item.get("placeholder", ""),
                "text": item.get("text", ""),
            }

        for inp in structure.get("inputs", []):
            add_element("input", inp)
        for btn in structure.get("buttons", []):
            add_element("btn", btn)
        for link in structure.get("links", []):
            add_element("link", link)

        # Update session element map (label -> selector) — not returned to LLM
        element_map.clear()
        for inp in structure.get("inputs", []):
            base = inp.get("semantic", "input")
            label = f"[input:{base}]"
            if label in element_map:
                cnt = 1
                while f"[input:{base}_{cnt}]" in element_map:
                    cnt += 1
                label = f"[input:{base}_{cnt}]"
            element_map[label] = inp["selector"]
        for btn in structure.get("buttons", []):
            base = btn.get("semantic", "btn")
            label = f"[btn:{base}]"
            if label in element_map:
                cnt = 1
                while f"[btn:{base}_{cnt}]" in element_map:
                    cnt += 1
                label = f"[btn:{base}_{cnt}]"
            element_map[label] = btn["selector"]
        for link in structure.get("links", []):
            base = link.get("semantic", "link")
            label = f"[link:{base}]"
            if label in element_map:
                cnt = 1
                while f"[link:{base}_{cnt}]" in element_map:
                    cnt += 1
                label = f"[link:{base}_{cnt}]"
            element_map[label] = link["selector"]

        total = len(elements)
        input_count = sum(1 for k in elements if k.startswith("[input:"))
        btn_count = sum(1 for k in elements if k.startswith("[btn:"))
        link_count = sum(1 for k in elements if k.startswith("[link:"))

        screenshot = await asyncio.wait_for(browser.screenshot(), timeout=15.0)
        from browser_mcp.tools import _save_screenshot
        path = _save_screenshot(screenshot)

        return {
            "status": "success",
            "elements": elements,
            "summary": f"页面包含 {input_count} 个输入框、{btn_count} 个按钮、{link_count} 个链接",
            "screenshot_path": path,
        }
    except asyncio.TimeoutError:
        raise ToolError(error_type="timeout", message="get_page_structure 超时")
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(error_type="unknown", message=f"获取页面结构失败: {e}")
