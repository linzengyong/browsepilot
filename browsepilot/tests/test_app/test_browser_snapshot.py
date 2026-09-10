"""Tier3 浏览器层单测：Page 池 + 无障碍树（aria snapshot）ref 解析。

这里**不启动真实浏览器**：
- `snapshot_to_refs` / `parse_snapshot_refs` 是 YAML 字符串上的纯函数（无 playwright 依赖）；
- `BrowserPool` 通过注入的 fake page 工厂驱动（page_factory 是池的测试缝）。
真实浏览器冒烟是独立脚本（brief Step 4），不入 pytest。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from browsepilot.app.browser import browser_pool as browser_pool_module
from browsepilot.app.browser.browser_pool import BrowserPool, page_snapshot_refs
from browsepilot.app.browser.snapshot import PageSnapshot, parse_snapshot_refs, snapshot_to_refs

# ── 样例快照 ──────────────────────────────────────────────────────────────────

# brief Step 1 的 YAML 块式样例（每条 ref 一个 "- ref: N" 块）。
SAMPLE_YAML = """- ref: 3
  role: button
  name: 搜索
  rect: [10, 20, 30, 40]
- ref: 4
  role: link
  name: 新闻
- ref: 5
  role: textbox
  name: 查询框
"""

# 真实 Playwright 1.59 输出（Chromium 实跑 page.locator("body").aria_snapshot(mode="ai")）：
# ref 标记 [ref=eN] 只在 mode="ai" 下产生；属性（[active]/[level=1]）排在 ref 之前；
# 可访问名里的引号被转义成 \"；ref 编号不保证连续。
ARIA_YAML = r"""- generic [active] [ref=e1]:
  - heading "Example Domain" [level=1] [ref=e2]
  - button "Say \"hi\"" [ref=e3]: Go
  - paragraph [ref=e7]: Some paragraph text here.
"""

# 带嵌套子行的结构（example.com 形态）：非 ref 行应随所属 ref 块保留。
ARIA_YAML_NESTED = r"""- generic [ref=e1]:
  - heading "Example Domain" [level=1] [ref=e2]
  - paragraph [ref=e3]:
    - text: This domain is for use in illustrative examples in documents.
    - link "More information..." [ref=e4]
"""


# ── snapshot_to_refs（纯函数） ────────────────────────────────────────────────


def test_snapshot_to_refs_keeps_ref_lines():
    out = snapshot_to_refs(SAMPLE_YAML, max_refs=2)
    assert "ref: 3" in out and "ref: 4" in out
    assert "ref: 5" not in out  # 截断
    assert out.count("ref:") == 2


def test_snapshot_max_refs_zero():
    assert snapshot_to_refs(SAMPLE_YAML, max_refs=0) == ""


def test_snapshot_to_refs_negative_max_refs_is_empty():
    # 防止把 max_refs 当切片索引用（[-1:] 会留下尾巴）
    assert snapshot_to_refs(SAMPLE_YAML, max_refs=-1) == ""


def test_snapshot_to_refs_keeps_whole_entry_block():
    out = snapshot_to_refs(SAMPLE_YAML, max_refs=1)
    assert "role: button" in out and "name: 搜索" in out  # 块内字段随 ref 一起保留
    assert "rect: [10, 20, 30, 40]" in out
    assert "role: link" not in out and "ref: 4" not in out  # 第二个条目整块丢弃


def test_snapshot_to_refs_keeps_all_entries_when_under_limit():
    out = snapshot_to_refs(SAMPLE_YAML, max_refs=30)
    assert out.count("ref:") == 3
    assert "name: 查询框" in out


def test_snapshot_to_refs_parses_playwright_ai_mode_refs():
    out = snapshot_to_refs(ARIA_YAML, max_refs=30)
    assert out.count("[ref=") == 4
    assert "[ref=e1]" in out and "[ref=e7]" in out


def test_snapshot_to_refs_truncates_aria_tree_at_entry_boundary():
    out = snapshot_to_refs(ARIA_YAML, max_refs=2)
    assert "[ref=e1]" in out and "[ref=e2]" in out
    assert "[ref=e3]" not in out and "[ref=e7]" not in out
    assert out.count("[ref=") == 2


def test_snapshot_to_refs_keeps_nested_child_lines_of_kept_entry():
    out = snapshot_to_refs(ARIA_YAML_NESTED, max_refs=3)
    assert "This domain is for use" in out  # e3 的 text 子行随块保留
    assert "More information" not in out  # 下一个 ref 及其行不越界


def test_snapshot_to_refs_without_refs_is_empty():
    assert snapshot_to_refs("- generic:\n  - text: 没有任何 ref", max_refs=30) == ""
    assert snapshot_to_refs("", max_refs=30) == ""


def test_snapshot_to_refs_ignores_ref_text_inside_quoted_name():
    # 原文按行原样保留，所以引号里那串 [ref=e9] 仍在输出里；关键是它不算一个 ref 条目。
    tree = '- heading "See [ref=e9] docs" [ref=e2]\n'
    refs = parse_snapshot_refs(tree)
    assert [r.label for r in refs] == ["e2"]
    assert snapshot_to_refs(tree, max_refs=1).count("[ref=e2]") == 1


def test_snapshot_ref_like_text_in_unquoted_value_is_not_a_phantom_ref():
    # reviewer 探针复现：'- text: use [ref=e12] to click' 里的 [ref=...] 是正文，不是条目头。
    # 幽灵条目会占掉 max_refs 名额，且 PageSnapshot.selector 会传出 aria-ref=e12 → 点错元素。
    tree = (
        "- generic [ref=e1]:\n"
        "  - paragraph [ref=e2]:\n"
        "    - text: use [ref=e12] to click\n"
    )

    refs = parse_snapshot_refs(tree)

    assert [r.label for r in refs] == ["e1", "e2"]  # 没有幽灵 e12 条目
    assert refs[1].selector == "aria-ref=e2"
    # 幽灵条目还会偷走一个 max_refs 名额：max_refs=2 时 e12 之后的行不该出现，
    # 而作为 e2 的细节子行必须保留。
    out = snapshot_to_refs(tree, max_refs=2)
    assert "use [ref=e12] to click" in out
    assert out.endswith("use [ref=e12] to click")


def test_snapshot_ref_like_text_only_line_yields_no_refs():
    # 只有正文行（无真实条目的快照）不得凭空造出 ref。
    tree = "- text: use [ref=e12] to click\n"
    assert parse_snapshot_refs(tree) == []
    assert snapshot_to_refs(tree, max_refs=30) == ""


# ── PageSnapshot / parse_snapshot_refs（给动作层的 locator 解析基础） ──────────


def test_parse_snapshot_refs_from_aria_tree():
    refs = parse_snapshot_refs(ARIA_YAML)
    assert [(r.label, r.ref_index) for r in refs] == [
        ("e1", 1),
        ("e2", 2),
        ("e3", 3),
        ("e7", 7),
    ]
    assert (refs[0].role, refs[0].name) == ("generic", "")
    assert (refs[1].role, refs[1].name) == ("heading", "Example Domain")
    assert (refs[2].role, refs[2].name) == ("button", 'Say "hi"')  # \" 反转义
    assert (refs[3].role, refs[3].name) == ("paragraph", "")
    assert refs[1].selector == "aria-ref=e2"  # T2 的 ref→locator 兜底链第一跳
    assert refs[1].line == '  - heading "Example Domain" [level=1] [ref=e2]'
    assert isinstance(refs[0], PageSnapshot)


def test_parse_snapshot_refs_from_block_yaml():
    refs = parse_snapshot_refs(SAMPLE_YAML)
    assert [r.label for r in refs] == ["3", "4", "5"]
    assert [r.ref_index for r in refs] == [3, 4, 5]
    assert [(r.role, r.name) for r in refs] == [
        ("button", "搜索"),
        ("link", "新闻"),
        ("textbox", "查询框"),
    ]
    assert refs[0].line == "- ref: 3"


def test_parse_snapshot_refs_skips_lines_without_ref():
    assert parse_snapshot_refs("- text: 无 ref 的文本行\n  - generic:\n") == []


# ── BrowserPool（fake page 工厂） ─────────────────────────────────────────────


class _FakePage:
    def __init__(self, index: int) -> None:
        self.index = index
        self.closed = False
        self.close_calls = 0
        self.url = "about:blank"

    async def close(self) -> None:
        self.closed = True
        self.close_calls += 1


class _FakePageFactory:
    """池的测试缝：记录每次发出的 page，替代真实 browser.new_page()。"""

    def __init__(self) -> None:
        self.created: list[_FakePage] = []

    async def __call__(self) -> Any:
        page = _FakePage(len(self.created) + 1)
        self.created.append(page)
        return page


def _make_pool(max_pages: int = 3) -> tuple[BrowserPool, _FakePageFactory]:
    factory = _FakePageFactory()
    return BrowserPool(max_pages=max_pages, page_factory=factory), factory


async def test_pool_rejects_invalid_max_pages():
    with pytest.raises(ValueError):
        BrowserPool(max_pages=0)


async def test_pool_new_page_requires_start():
    pool, _ = _make_pool()
    with pytest.raises(RuntimeError):
        await pool.new_page()


async def test_pool_new_page_creates_page_after_start():
    pool, factory = _make_pool()
    await pool.start()
    assert factory.created == []  # 工厂只发给 page 请求，start 本身不建 page
    page = await pool.new_page()
    assert factory.created == [page]
    assert pool.pages_created == 1 and pool.pages_reused == 0
    await pool.close()


async def test_pool_reuses_released_page():
    pool, factory = _make_pool(max_pages=2)
    await pool.start()
    p1 = await pool.new_page()
    p2 = await pool.new_page()

    await pool.release(p1)
    p3 = await pool.new_page()

    assert p3 is p1  # 复用，而不是新建
    assert p1.closed is False  # release 不销毁 page
    assert len(factory.created) == 2  # 复用计数：只建过 2 个 page
    assert (pool.pages_created, pool.pages_reused, pool.pages_evicted) == (2, 1, 0)
    assert p2 is not p3
    await pool.close()


async def test_pool_evicts_oldest_page_at_capacity():
    pool, factory = _make_pool(max_pages=2)
    await pool.start()
    p1 = await pool.new_page()
    p2 = await pool.new_page()

    p3 = await pool.new_page()  # 到上限：回收最老的 page，保证调用方拿到可用 page

    assert p1.closed is True and p1.close_calls == 1
    assert p2.closed is False
    assert p3 is not p1 and p3 is not p2
    assert len(factory.created) == 3
    assert pool.pages_evicted == 1
    await pool.close()


class _ConcurrencyProbeFactory:
    """记录“新建时池中仍存活（已创建且未 close）的 page 数”高水位。

    没有串行化时，5 个并发 ``new_page()`` 会全部悬在 ``await _create_page()`` 上
    （检查与 append 之间隔着 await）→ 高水位 = 5（reviewer 探针实测 pages_created=5, busy=5）。
    """

    def __init__(self) -> None:
        self.created: list[_FakePage] = []
        self.live_high_water = 0

    @property
    def live(self) -> int:
        return sum(1 for page in self.created if not page.closed)

    async def __call__(self) -> Any:
        self.live_high_water = max(self.live_high_water, self.live + 1)
        page = _FakePage(len(self.created) + 1)
        self.created.append(page)
        await asyncio.sleep(0)  # 让出控制权：把“检查 → 创建 → append”之间的窗口放大
        return page


async def test_pool_concurrent_new_page_respects_max_pages():
    factory = _ConcurrencyProbeFactory()
    pool = BrowserPool(max_pages=2, page_factory=factory)
    await pool.start()

    pages = await asyncio.gather(*(pool.new_page() for _ in range(5)))

    # 池中同时存活的 page 至多 max_pages 个（无串行化实现：live_high_water == 5）
    assert factory.live_high_water <= pool.max_pages, f"live pages: {factory.live_high_water}"
    assert all(page in factory.created for page in pages)  # 每个请求拿到池里真实发出的 page
    assert factory.live <= pool.max_pages
    # 5 个请求都被满足，但多出的只能靠“回收最老”腾位：2 个存活 + 3 个已回收关闭
    assert (pool.pages_created, pool.pages_reused, pool.pages_evicted) == (5, 0, 3)

    await pool.close()
    assert all(page.closed for page in factory.created)  # 无孤儿：建过的每个 page 最终都关了
    assert factory.live == 0


async def test_pool_release_unknown_page_is_noop():
    pool, factory = _make_pool(max_pages=2)
    await pool.start()
    page = await pool.new_page()

    await pool.release(_FakePage(99))  # 不属于本池 → 静默忽略，不改池状态

    assert (pool.pages_created, pool.pages_reused) == (1, 0)
    assert page.closed is False
    other = await pool.new_page()  # 真 page 仍算占用 → 走新建而非复用
    assert other is not page
    assert len(factory.created) == 2
    await pool.close()


async def test_pool_never_hands_out_an_error_page():
    """F4（T6 冒烟 S6）：失败导航留下的 ``chrome-error://`` 页面**绝不复用**。

    错误页自身的导航可能还没落定，复用它的下一次 ``goto`` 会报
    "interrupted by another navigation to chrome-error://chromewebdata/"（假失败）；
    所以交付前判一次：碰到错误页就弃用（关闭）并新建。
    """
    pool, factory = _make_pool(max_pages=2)
    await pool.start()
    page = await pool.new_page()
    page.url = "chrome-error://chromewebdata/"  # 失败导航之后浏览器自己跳到这里
    await pool.release(page)

    fresh = await pool.new_page()

    assert fresh is not page  # 拿到的是干净新页
    assert page.closed is True and page.close_calls == 1
    assert (pool.pages_created, pool.pages_reused, pool.pages_discarded) == (2, 0, 1)
    await pool.close()


async def test_pool_skips_error_page_that_became_dirty_while_idle():
    """归还时还没脏（错误页导航晚一步落定）的页面，也不能被发出去。"""
    pool, factory = _make_pool(max_pages=2)
    await pool.start()
    page = await pool.new_page()
    await pool.release(page)  # 正常归还：此时 URL 还是 https://…
    page.url = "chrome-error://chromewebdata/"  # 归还之后才被错误页导航带走

    fresh = await pool.new_page()

    assert fresh is not page
    assert page.closed is True
    assert pool.pages_discarded == 1
    await pool.close()


async def test_pool_discard_closes_the_page_and_frees_its_slot():
    """F4：executor 弃用失败导航的页面走 ``discard()``——关掉它，并且不占池槽。"""
    pool, factory = _make_pool(max_pages=1)
    await pool.start()
    page = await pool.new_page()

    await pool.discard(page)

    assert page.closed is True
    assert pool.pages_discarded == 1
    other = await pool.new_page()  # max_pages=1：槽位真释放了（走新建，不是回收）
    assert other is not page
    assert pool.pages_evicted == 0
    await pool.close()


async def test_pool_discard_unknown_page_is_noop():
    pool, factory = _make_pool(max_pages=2)
    await pool.start()
    page = await pool.new_page()
    stranger = _FakePage(99)

    await pool.discard(stranger)  # 不属于本池 → 静默忽略（也不关它）

    assert stranger.closed is False
    assert pool.pages_discarded == 0
    assert page.closed is False
    await pool.close()


async def test_pool_close_closes_all_pages_once_and_is_idempotent():
    pool, _ = _make_pool(max_pages=3)
    await pool.start()
    busy = await pool.new_page()
    idle = await pool.new_page()
    await pool.release(idle)  # 空闲 page 同样要被 close

    await pool.close()

    assert busy.closed is True and idle.closed is True
    assert (busy.close_calls, idle.close_calls) == (1, 1)
    await pool.close()  # 幂等
    with pytest.raises(RuntimeError):
        await pool.new_page()


async def test_pool_start_is_idempotent_and_can_restart_after_close():
    pool, factory = _make_pool(max_pages=1)
    await pool.start()
    await pool.start()  # 幂等：不重复启动

    p1 = await pool.new_page()
    await pool.close()
    assert p1.closed is True

    await pool.start()  # 关池后可重新 start（新生命周期）
    p2 = await pool.new_page()
    assert p2 is not p1
    assert len(factory.created) == 2
    await pool.close()


# ── start() 失败路径：playwright driver 不得泄漏（fake 模块级 async_playwright） ──


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False
        self.pages: list[Any] = []

    async def new_page(self) -> Any:
        page = _FakePage(len(self.pages) + 1)
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, *, fail_launch: bool) -> None:
        self._fail_launch = fail_launch
        self.launch_calls = 0

    async def launch(self, **kwargs: Any) -> _FakeBrowser:
        self.launch_calls += 1
        if self._fail_launch:
            raise RuntimeError("fake chromium launch failed")
        return _FakeBrowser()


class _FakePlaywrightHandle:
    """替身 playwright driver：``chromium.launch`` 可编程失败，``stop()`` 留痕。"""

    def __init__(self, *, fail_launch: bool) -> None:
        self.chromium = _FakeChromium(fail_launch=fail_launch)
        self.stopped = False
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stopped = True
        self.stop_calls += 1


class _FakePlaywrightLauncher:
    """替代模块级 ``async_playwright``：``()`` 返回自身，``start()`` 发一个新 driver。

    前 ``fail_launches`` 个 driver 的 ``chromium.launch`` 抛异常（模拟启动失败）。
    """

    def __init__(self, *, fail_launches: int = 0) -> None:
        self.handles: list[_FakePlaywrightHandle] = []
        self._fail_launches = fail_launches

    def __call__(self) -> _FakePlaywrightLauncher:
        return self

    async def start(self) -> _FakePlaywrightHandle:
        handle = _FakePlaywrightHandle(fail_launch=self._fail_launches > 0)
        self._fail_launches = max(0, self._fail_launches - 1)
        self.handles.append(handle)
        return handle


async def test_pool_start_failure_stops_driver_and_retry_succeeds(monkeypatch):
    launcher = _FakePlaywrightLauncher(fail_launches=1)
    monkeypatch.setattr(browser_pool_module, "async_playwright", launcher)
    pool = BrowserPool(max_pages=1)

    with pytest.raises(RuntimeError, match="fake chromium launch failed"):
        await pool.start()

    # 失败路径不得留下 driver 引用（否则重试会再起一个，第一个永久泄漏）
    assert pool._playwright is None
    assert pool._browser is None
    assert pool._started is False
    assert launcher.handles[0].stopped is True  # 已启动的 driver 必须被 stop
    assert launcher.handles[0].stop_calls == 1

    await pool.start()  # 重试：新 driver 正常启动，无残留
    page = await pool.new_page()

    assert pool._started is True
    assert pool._playwright is launcher.handles[1]
    assert launcher.handles[1].stopped is False  # 在用 driver 不受影响
    assert page.closed is False

    await pool.close()
    assert launcher.handles[1].stopped is True
    assert page.closed is True  # 无残留 page
    assert launcher.handles[0].stop_calls == 1  # 失败路径只 stop 一次


# ── page_snapshot_refs（页面侧 helper：ai 模式 + 边界截断） ────────────────────


class _FakeLocator:
    def __init__(self, tree: str, calls: list[dict[str, Any]]) -> None:
        self._tree = tree
        self._calls = calls

    async def aria_snapshot(
        self,
        *,
        mode: str | None = None,
        depth: int | None = None,
        timeout: float | None = None,
    ) -> str:
        self._calls.append({"mode": mode, "depth": depth})
        return self._tree


class _FakeSnapshotPage:
    """只实现 page_snapshot_refs 用到的部分（locator() 是同步 API，与 Playwright 一致）。"""

    def __init__(self, tree: str) -> None:
        self._tree = tree
        self.roots: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def locator(self, selector: str) -> _FakeLocator:
        self.roots.append(selector)
        return _FakeLocator(self._tree, self.calls)


async def test_page_snapshot_refs_uses_ai_mode_and_truncates():
    page = _FakeSnapshotPage(ARIA_YAML)

    out = await page_snapshot_refs(page, max_refs=2)

    assert page.roots == ["body"]
    assert page.calls[0]["mode"] == "ai"  # 只有 ai 模式才带 [ref=eN]
    assert out.count("[ref=") == 2
    assert "[ref=e3]" not in out


async def test_page_snapshot_refs_respects_root_and_default_max_refs():
    page = _FakeSnapshotPage(SAMPLE_YAML)

    out = await page_snapshot_refs(page, root="main")

    assert page.roots == ["main"]
    assert out.count("ref:") == 3  # 默认 max_refs=30 > 3 条
