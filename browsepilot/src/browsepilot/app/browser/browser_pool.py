"""Tier3 浏览器层：Page 池 + 页面侧快照 helper。

``BrowserPool`` 管理一个 Chromium 与至多 ``max_pages`` 个 page（复用 / 回收）：

- ``start()``：启动 playwright + 浏览器（幂等；注入 ``page_factory`` 时只标记启动，供单测）；
- ``new_page()``：优先复用 ``release()`` 归还的空闲 page，否则新建；到上限时回收最老的 page；
- ``release(page)``：把 page 归还池中复用；
- ``discard(page)``：关闭并弃用（**不回池**；失败导航后的浏览器错误页走这里，F4）；
- ``close()``：关闭全部 page + 浏览器 + playwright（幂等）。

**错误页绝不复用**（F4 / T6 冒烟 S6 确定性复现）：一次失败的 ``goto`` 之后浏览器把页面带到
``chrome-error://chromewebdata/``，而这个错误页导航可能还没落定——复用同一页的下一次 ``goto``
会报 "interrupted by another navigation to chrome-error://chromewebdata/"（假失败，连累下一次
``browser_open``）。所以 ``new_page()`` 把 idle 页交出去之前先看 URL，是错误页就弃用改用新页
（归还时还来不及变脏的也遮住）；executor 在失败导航后则显式 ``discard()``——它自己拿着当前页，
不经池。

``channel`` 是给真实环境的逃生门：Playwright 1.59 默认要 chromium rev 1217，而本机浏览器缓存
只有 1208（默认 headless 启动报 "Executable doesn't exist"），用系统 Edge 即可
（``BrowserPool(headless=True, channel="msedge")``，实测可跑）。

快照 API 的实测事实：``[ref=eN]`` 只在 ``aria_snapshot(mode="ai")`` 下产生，
页面侧统一走 :func:`page_snapshot_refs`。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from playwright.async_api import Browser, Page, Playwright, async_playwright

from browsepilot.app.browser.snapshot import snapshot_to_refs

__all__ = ["BrowserPool", "page_snapshot_refs"]

PageFactory = Callable[[], Awaitable[Page]]

_ERROR_PAGE_PREFIX = "chrome-error://"
"""浏览器错误页的 scheme 前缀（Chromium/Edge 都是 ``chrome-error://chromewebdata/``）。

失败导航（DNS 失败 / 连接拒绝）后页面会自己停在这里：它不是“当前页”，不能复用。
"""


def _is_error_page(page: Any) -> bool:
    """页面是否停在浏览器错误页（读不出 URL 的替身 / 驱动异常 → 按“不是”处理，宁可复用）。"""
    try:
        return str(page.url).startswith(_ERROR_PAGE_PREFIX)
    except Exception:
        return False


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
        # 串行化“检查 → 创建”：new_page 在检查与 append 之间要 await 建页，
        # 无锁时并发调用会全部看到“未到上限”而各建一个 page，突破 max_pages。
        self._new_page_lock = asyncio.Lock()
        # 观测计数（复用 / 回收行为可直接断言）
        self.pages_created = 0
        self.pages_reused = 0
        self.pages_evicted = 0
        self.pages_discarded = 0  # F4：失败导航后的错误页 / 显式 discard()

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 playwright + 浏览器；重复调用是空操作。

        启动失败时不留 driver（已启动的 playwright 会被 stop），故失败后可直接重试。
        """
        if self._started:
            return
        if self._page_factory is None:
            self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless, channel=self.channel
                )
            except Exception:
                # launch 失败时 driver 已经起来了：不 stop 就永泄漏（重试还会再起一个）。
                await self._discard_playwright()
                raise
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
            "BrowserPool closed (created={}, reused={}, evicted={}, discarded={})",
            self.pages_created,
            self.pages_reused,
            self.pages_evicted,
            self.pages_discarded,
        )

    # ── page 复用 / 回收 ──────────────────────────────────────────────────────

    async def new_page(self) -> Page:
        """取一个可用 page：先复用空闲 page，再新建，到上限则回收最老的 page。

        “检查 + 创建”整体在 ``_new_page_lock`` 内串行：并发调用不会同时看到“未到上限”
        而各自 await 出一个新 page（那会突破 ``max_pages``）。

        idle 里的**错误页**（``chrome-error://``）直接弃用、继续看下一个（F4）：失败导航的
        页面会自己跳到错误页，而复用它就会撞上错误页自身的导航（见模块 docstring）。
        """
        if not self._started:
            raise RuntimeError("BrowserPool.new_page() called before start()")
        async with self._new_page_lock:
            if not self._started:  # 等锁期间池被 close()
                raise RuntimeError("BrowserPool.new_page() called before start()")
            while self._idle:
                page = self._idle.pop(0)
                if not _is_error_page(page):
                    self._busy.append(page)
                    self.pages_reused += 1
                    logger.debug("BrowserPool reused idle page (reused={})", self.pages_reused)
                    return page
                logger.warning(
                    "BrowserPool dropping an idle error page ({}); creating a clean one",
                    _ERROR_PAGE_PREFIX,
                )
                await self._dispose(page)
                self.pages_discarded += 1
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

    async def discard(self, page: Page) -> None:
        """弃用页面（关闭 + 彻底释放池槽；未知 page 静默忽略）。

        executor（F4）在**失败导航**后用这个而不是 ``release()``：失败导航的页面停在浏览器
        错误页，而错误页自身的导航可能还在进行——回 idle 会让下一个消费者拿到它并撞上
        "interrupted by another navigation"（T6 S6）。discard 不可逆，但它不破坏“失败即数据”：
        动作结果已经由 executor 收好，这一页本来就不可信了。
        """
        for pages in (self._busy, self._idle):
            for index, candidate in enumerate(pages):
                if candidate is not page:
                    continue
                del pages[index]
                await self._dispose(page)
                self.pages_discarded += 1
                return

    # ── 内部 ──────────────────────────────────────────────────────────────────

    async def _discard_playwright(self) -> None:
        """启动失败路径：停止并丢弃 driver（不碰 idle/busy 与 ``_started``）。"""
        self._browser = None
        playwright, self._playwright = self._playwright, None
        if playwright is None:
            return
        try:
            await playwright.stop()
        except Exception as exc:  # driver 可能已自行退出
            logger.warning("Error stopping playwright after failed start: {}", exc)

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
