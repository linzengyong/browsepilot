"""Navigate tool — go to a URL."""

import asyncio

from fastmcp.server.context import Context
from browser_mcp.server import mcp
from browser_mcp.tools import ToolError, _save_screenshot, validate_url


@mcp.tool()
async def navigate(url: str, ctx: Context) -> dict:
    """导航到指定 URL。URL 必须包含完整协议（https://），或使用最近一次 get_page_structure 中链接的完整 href。"""
    browser = ctx.request_context.lifespan_context["browser"]
    is_valid, error = validate_url(url)
    if not is_valid:
        raise ToolError("invalid_input", error)
    page = await browser.get_page()
    try:
        await asyncio.wait_for(page.goto(url, wait_until="domcontentloaded"), timeout=15)
        title = await page.title()

        # Extract visible text preview (browser-side truncation to avoid full DOM serialization)
        try:
            visible_text_preview = await page.evaluate("document.body.innerText.slice(0, 200)") or ""
        except Exception:
            visible_text_preview = ""

        # Detect common issues
        detected_issues = []
        page_text = (visible_text_preview + title).lower()
        if any(kw in page_text for kw in ["验证", "captcha", "人机验证"]):
            detected_issues.append("captcha")
        if any(kw in page_text for kw in ["登录", "login", "sign in"]):
            detected_issues.append("login_wall")
        if any(kw in page_text for kw in ["弹出", "popup"]):
            detected_issues.append("popup")
        if "challenge" in page.url.lower() or "cf-browser-verify" in page_text:
            detected_issues.append("cloudflare")

        screenshot = await browser.screenshot()
        return {
            "status": "success",
            "screenshot_path": _save_screenshot(screenshot),
            "title": title,
            "visible_text_preview": visible_text_preview,
            "detected_issues": detected_issues,
        }
    except asyncio.TimeoutError:
        screenshot = await browser.screenshot()
        raise ToolError("timeout", f"导航到 {url} 超时")
    except Exception as e:
        screenshot = await browser.screenshot()
        raise ToolError("navigation_failed", str(e))
