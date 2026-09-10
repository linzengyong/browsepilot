"""Tier3 浏览器层：Page 池 + 页面侧快照 helper。

``BrowserPool`` 管理一个 Chromium 与至多 ``max_pages`` 个 page（复用 / 回收）：

- ``start()``：启动 playwright + 浏览器（幂等；注入 ``page_factory`` 时只标记启动，供单测）；
- ``new_page()``：优先复用 ``release()`` 归还的空闲 page，否则新建；到上限时回收最老的 page；
- ``release(page)``：把 page 归还池中复用；
- ``close()``：关闭全部 page + 浏览器 + playwright（幂等）。

``channel`` 是给真实环境的逃生门：Playwright 1.59 默认要 chromium rev 1217，而本机浏览器缓存
只有 1208（默认 headless 启动报 "Executable doesn't exist"），用系统 Edge 即可
（``BrowserPool(headless=True, channel="msedge")``，实测可跑）。

快照 API 的实测事实：``[ref=eN]`` 只在 ``aria_snapshot(mode="ai")`` 下产生，
页面侧统一走 :func:`page_snapshot_refs`。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from playwright.async_api import Browser, Page, Playwright, async_playwright

from browsepilot.app.browser.snapshot import snapshot_to_refs

__all__ = ["BrowserPool", "page_snapshot_refs"]

PageFactory = Callable[[], Awaitable[Page]]


class BrowserPool:
    """至多 ``max_pages`` 个 page 的浏览器池，供 Tier3 动作层复用。"""

    def __init__(
        self,
        headless: bool = True,
        max_pages: int = 3,
        *,
        channel: str | None = None,
        page_factory: PageFactory | None = None,
    ) -> None:
        if max_pages < 1:
            raise ValueError(f"max_pages must be >= 1, got {max_pages}")
        self.headless = headless
        self.max_pages = max_pages
        self.channel = channel
        self._page_factory = page_factory
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._idle: list[Any] = []
        self._busy: list[Any] = []
        self._started = False
        # 观测计数（复用 / 回收行为可直接断言）
        self.pages_created = 0
        self.pages_reused = 0
        self.pages_evicted = 0

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 playwright + 浏览器；重复调用是空操作。"""
        if self._started:
            return
        if self._page_factory is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless, channel=self.channel
            )
            logger.info(
                "BrowserPool started (headless={}, channel={}, max_pages={})",
                self.headless,
                self.channel or "bundled",
                self.max_pages,
            )
        self._idle.clear()
        self._busy.clear()
        self._started = True

    async def close(self) -> None:
        """关闭池中所有 page 与浏览器；重复调用是空操作。"""
        if not self._started and self._browser is None and self._playwright is None:
            return
        pages = [*self._busy, *self._idle]
        self._busy.clear()
        self._idle.clear()
        for page in pages:
            await self._dispose(page)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:  # 浏览器可能已被外部关掉
                logger.warning("Error closing browser: {}", exc)
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.warning("Error stopping playwright: {}", exc)
            self._playwright = None
        self._started = False
        logger.info(
            "BrowserPool closed (created={}, reused={}, evicted={})",
            self.pages_created,
            self.pages_reused,
            self.pages_evicted,
        )

    # ── page 复用 / 回收 ──────────────────────────────────────────────────────

    async def new_page(self) -> Page:
        """取一个可用 page：先复用空闲 page，再新建，到上限则回收最老的 page。"""
        if not self._started:
            raise RuntimeError("BrowserPool.new_page() called before start()")
        if self._idle:
            page = self._idle.pop(0)
            self._busy.append(page)
            self.pages_reused += 1
            logger.debug("BrowserPool reused idle page (reused={})", self.pages_reused)
            return page
        if len(self._busy) < self.max_pages:
            page = await self._create_page()
            self._busy.append(page)
            return page
        evicted = self._busy.pop(0)
        logger.warning(
            "BrowserPool at capacity ({} pages); recycling the oldest page", self.max_pages
        )
        await self._dispose(evicted)
        self.pages_evicted += 1
        page = await self._create_page()
        self._busy.append(page)
        return page

    async def release(self, page: Page) -> None:
        """把 page 归还池中复用（未知 page 静默忽略；池已关则直接销毁）。"""
        for index, candidate in enumerate(self._busy):
            if candidate is not page:
                continue
            del self._busy[index]
            if self._started:
                self._idle.append(page)
            else:
                await self._dispose(page)
            return

    # ── 内部 ──────────────────────────────────────────────────────────────────

    async def _create_page(self) -> Page:
        if self._page_factory is not None:
            page = await self._page_factory()
        else:
            assert self._browser is not None  # start() 已保证
            page = await self._browser.new_page()
        self.pages_created += 1
        return page

    async def _dispose(self, page: Page) -> None:
        try:
            await page.close()
        except Exception as exc:  # 已被外部关闭 / 浏览器已退出
            logger.debug("Ignoring page close error: {}", exc)


async def page_snapshot_refs(page: Page, *, max_refs: int = 30, root: str = "body") -> str:
    """抓当前页无障碍树快照 → ≤ ``max_refs`` 条 ref 行（工具 ``browser_snapshot`` 的语义）。

    必须用 ``mode="ai"``：这是唯一会输出 ``[ref=eN]`` 标记的模式（Playwright 1.59 实测），
    动作层正是靠 ``aria-ref=eN`` 选择器把这些 ref 定位回元素。
    """
    tree = await page.locator(root).aria_snapshot(mode="ai")
    return snapshot_to_refs(tree, max_refs=max_refs)
