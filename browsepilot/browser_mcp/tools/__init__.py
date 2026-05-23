"""MCP tool implementations for browser automation."""

import base64
import os
import re
from fnmatch import fnmatch
from urllib.parse import urlparse

import aiofiles

ALLOWED_DOMAINS: list[str] = []


def set_allowed_domains(domains: list[str]) -> None:
    global ALLOWED_DOMAINS
    ALLOWED_DOMAINS = domains


def _match_hostname(hostname: str, pattern: str) -> bool:
    """Check if hostname matches a domain pattern.

    Supports fnmatch wildcards (*, ?). When the pattern contains wildcards,
    fnmatch is used. Otherwise falls back to exact match + subdomain match.
    """
    if "*" in pattern or "?" in pattern:
        return fnmatch(hostname, pattern)
    return hostname == pattern or hostname.endswith("." + pattern)


def validate_url(url: str) -> tuple:
    """Validate URL against security policy. Returns (is_valid, error_message)."""
    if not isinstance(url, str):
        return False, "invalid_input: URL must be a string"
    if url.startswith("file://") or url.startswith("file:"):
        return False, "protocol_blocked: file:// protocol is not allowed"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"protocol_blocked: {parsed.scheme} is not allowed"
    if not ALLOWED_DOMAINS:
        return True, ""
    hostname = parsed.hostname or ""
    allowed = any(
        _match_hostname(hostname, domain)
        for domain in ALLOWED_DOMAINS
    )
    if not allowed:
        return False, f"domain_not_allowed: {hostname} is not in whitelist"
    return True, ""


_blocked_pattern = re.compile(
    r'\b(eval|fetch|XMLHttpRequest|WebSocket|localStorage|sessionStorage)\b',
    re.IGNORECASE
)


def filter_js_script(script: str) -> tuple:
    """Validate JS script safety. Returns (is_safe, error_message)."""
    if not isinstance(script, str):
        return False, "invalid_input: script must be a string"
    m = _blocked_pattern.search(script)
    if m:
        return False, f"script_blocked: '{m.group(1)}' is not allowed"
    return True, ""


async def _save_screenshot(screenshot_base64: str, session_id: str = "", step_index: int = 0, data_dir: str = "data") -> str:
    """Save base64 screenshot to disk (async I/O via aiofiles), return file path."""
    if not screenshot_base64:
        return ""
    screenshots_dir = os.path.join(data_dir, "screenshots", session_id) if session_id else os.path.join(data_dir, "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)
    path = os.path.join(screenshots_dir, f"step_{step_index}.png")
    async with aiofiles.open(path, "wb") as f:
        await f.write(base64.b64decode(screenshot_base64))
    return path


def _get_selector_map(ctx) -> dict:
    """Get element ref -> CSS selector from session context."""
    try:
        return ctx.request_context.lifespan_context.get("element_map", {})
    except Exception:
        return {}


def _get_screenshot_meta(ctx) -> tuple[str, int]:
    """Extract (session_id, step_index) from request meta for screenshot naming."""
    try:
        meta = getattr(ctx.request_context, "meta", None) or {}
        return meta.get("session_id", ""), meta.get("step_index", 0)
    except Exception:
        return "", 0


class ToolError(Exception):
    """Structured error for LLM-friendly MCP tool failures."""

    def __init__(self, error_type: str, message: str, available_refs: list[str] | None = None):
        self.error_type = error_type
        self.message = message
        self.available_refs = available_refs or []
        super().__init__(message)

