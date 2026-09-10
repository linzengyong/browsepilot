"""Tier3 浏览器层单测：Page 池 + 无障碍树（aria snapshot）ref 解析。

这里**不启动真实浏览器**：
- `snapshot_to_refs` / `parse_snapshot_refs` 是 YAML 字符串上的纯函数（无 playwright 依赖）；
- `BrowserPool` 通过注入的 fake page 工厂驱动（page_factory 是池的测试缝）。
真实浏览器冒烟是独立脚本（brief Step 4），不入 pytest。
"""

from __future__ import annotations

from typing import Any

import pytest

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
