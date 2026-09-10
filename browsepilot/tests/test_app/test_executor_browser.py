"""T4 接线单测：AppToolExecutor 的 6 个 Tier3 浏览器工具。

覆盖三条主线：**policy 门**（navigate 之前把关、拒绝路径绝不建页/导航）、
**当前页跟踪**（open 复用同一个 page；snapshot/click/type/scroll 都作用在它身上）、
**结构化降级**（未注入池 / 池未启动 / 快照为空 / 动作失败 一律返回 ToolResult，绝不抛）。

不启真实浏览器（教训 §1）：FakeBrowser / FakePage 双人舞——page 的 ``calls`` 保留跨方法
的时间顺序（用来 pin "先 aria-ref 定位再 click"、"type 先清空再填"），失败可编程
（goto / networkidle 等待 / snapshot / click / JS 回退 / evaluate）；FakeBrowser 只实现
executor 用到的 ``new_page()``，并记录建页次数（当前页复用与并发串行化的证据）。
真浏览器冒烟是 T6 的独立脚本，不入 pytest。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from browsepilot.agent.tools import ToolExecutor
from browsepilot.app.browser.policy import NavigationPolicy
from browsepilot.app.executor import MAX_JS_RESULT_CHARS, AppToolExecutor
from browsepilot.app.services.weather import Weather

BROWSER_TOOLS = [
    "browser_open",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_execute_js",
]

# Playwright 1.59 ai 模式实况（见 test_browser_snapshot.py）：[ref=eN] 只在 mode="ai" 下产生。
ARIA_YAML = """- generic [ref=e1]:
  - heading "Example Domain" [level=1] [ref=e2]
  - link "More information..." [ref=e4]
"""


# ── fake page / browser（executor 的测试缝） ──────────────────────────────────


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self._page = page
        self.selector = selector

    async def aria_snapshot(self, *, mode: str = "ai") -> str:
        self._page.calls.append(("aria_snapshot", self.selector, mode))
        if self._page.fail_snapshot:
            raise RuntimeError("aria snapshot unavailable")
        return self._page.snapshot_tree

    async def click(self, *, timeout: float | None = None) -> None:
        self._page.calls.append(("click", self.selector))
        if self._page.fail_click:
            raise PlaywrightTimeoutError(f"click timeout: {self.selector}")

    async def fill(self, value: str, *, timeout: float | None = None) -> None:
        self._page.calls.append(("fill", self.selector, value))

    async def evaluate(self, expression: Any, arg: Any = None, *, timeout: float | None = None):
        self._page.calls.append(("locator_evaluate", self.selector, expression))
        if self._page.fail_locator_evaluate:
            raise PlaywrightTimeoutError(f"js fallback timeout: {self.selector}")
        return True

    async def scroll_into_view_if_needed(self, *, timeout: float | None = None) -> None:
        self._page.calls.append(("scroll_into_view", self.selector))


class FakeMouse:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        self._page.calls.append(("wheel", delta_x, delta_y))


class FakePage:
    """记录调用序、可编程失败的假 page（只实现 executor/actions 真正用到的方法）。"""

    def __init__(
        self,
        *,
        url: str = "https://example.com/",
        snapshot_tree: str = ARIA_YAML,
        js_result: Any = "Example Domain",
    ) -> None:
        self.url = url
        self.snapshot_tree = snapshot_tree
        self.js_result = js_result
        self.calls: list[tuple] = []
        self.goto_calls: list[str] = []
        self.fail_goto = False
        self.fail_networkidle_wait = False
        self.fail_snapshot = False
        self.fail_click = False
        self.fail_locator_evaluate = False
        self.fail_evaluate = False
        self.mouse = FakeMouse(self)

    async def goto(self, url: str, *, wait_until: str | None = None, timeout: float | None = None):
        self.goto_calls.append(url)
        self.calls.append(("goto", url, wait_until))
        if self.fail_goto:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
        self.url = url
        return FakeResponse()

    async def wait_for_load_state(self, state: str | None = None, *, timeout: float | None = None):
        self.calls.append(("wait_for_load_state", state))
        if self.fail_networkidle_wait:
            raise PlaywrightTimeoutError("networkidle wait bounded out")

    def locator(self, selector: str) -> FakeLocator:
        self.calls.append(("locator", selector))
        return FakeLocator(self, selector)

    def get_by_text(self, text: str, *, exact: bool | None = False) -> FakeLocator:
        self.calls.append(("get_by_text", text, exact))
        return FakeLocator(self, f"text={text}")

    async def evaluate(self, expression: Any):
        self.calls.append(("evaluate", expression))
        if self.fail_evaluate:
            raise RuntimeError("Evaluation failed: ReferenceError")
        return self.js_result


class FakeBrowser:
    """BrowserPool 替身：只实现 executor 用到的 new_page()；可注入启动失败。"""

    def __init__(self, page: FakePage | None = None, *, start_error: Exception | None = None):
        self._page = page
        self.start_error = start_error
        self.created: list[FakePage] = []
        self.new_page_calls = 0

    async def start(self) -> None:  # pragma: no cover - 池生命周期由 app 接线负责
        pass

    async def close(self) -> None:  # pragma: no cover
        pass

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        await asyncio.sleep(0)  # 真实建页有 await 点：并发测试需要这个让出点
        if self.start_error is not None:
            raise self.start_error
        page = self._page if self._page is not None else FakePage()
        self.created.append(page)
        return page


# ── executor 骨架（Tier1/2 服务只是占位：本文件只测 Tier3 接线） ────────────────


class FakeWeather:
    async def get(self, city: str) -> Weather:
        return Weather(city=city, temperature_c=25.0, conditions="晴")


class FakeSearch:
    async def search(self, query: str, max_results: int = 5):
        return []


class FakeFetch:
    async def fetch(self, url: str):
        return None


def make_executor(browser: Any = None, policy: NavigationPolicy | None = None) -> AppToolExecutor:
    return AppToolExecutor(
        FakeWeather(), FakeSearch(), FakeFetch(), browser=browser, policy=policy
    )


# ── 工具清单 / 无池降级 ──────────────────────────────────────────────────────


def test_executor_with_browser_still_implements_protocol():
    assert isinstance(make_executor(FakeBrowser()), ToolExecutor)


@pytest.mark.asyncio
async def test_browser_tools_listed_with_parameters():
    tools = {t.name: t for t in await make_executor(FakeBrowser()).list_tools()}
    assert set(BROWSER_TOOLS) <= set(tools)
    assert tools["browser_open"].parameters == {"url": "string"}
    assert tools["browser_click"].parameters == {"ref": "string"}
    assert tools["browser_type"].parameters == {"ref": "string", "text": "string"}
    assert tools["browser_scroll"].parameters == {"direction": "string"}
    assert tools["browser_execute_js"].parameters == {"expression": "string"}
    assert all(tools[name].description for name in BROWSER_TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("browser_open", {"url": "https://example.com/"}),
        ("browser_snapshot", {}),
        ("browser_click", {"ref": "e2"}),
        ("browser_type", {"ref": "e2", "text": "x"}),
        ("browser_scroll", {"direction": "down"}),
        ("browser_execute_js", {"expression": "document.title"}),
    ],
)
async def test_browser_tools_without_pool_degrade_structured(tool, arguments):
    """未注入 BrowserPool：6 个工具都必须返回明确的结构化错误（绝不抛异常）。"""
    result = await make_executor().call(tool, arguments)
    assert result.status == "error"
    assert result.error_type == "browser_unavailable"
    assert "浏览器" in result.error_message


# ── policy 门（navigate 前把关） ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected_code"),
    [
        ("file:///etc/passwd", "protocol_blocked"),
        ("javascript:alert(1)", "protocol_blocked"),
        ("http://127.0.0.1:8000/admin", "private_address_blocked"),
        ("http://169.254.169.254/latest/meta-data/", "private_address_blocked"),
        ("http://localhost:5173/", "private_address_blocked"),
        (" https://example.com/", "invalid_url"),
    ],
)
async def test_browser_open_gates_url_before_navigating(url, expected_code):
    """T2 交接：actions.navigate 自身不设防（可开 file://），把关必须在 executor 这一层。"""
    browser = FakeBrowser()
    result = await make_executor(browser).call("browser_open", {"url": url})
    assert result.status == "error"
    assert result.error_type == "navigation_blocked"
    assert expected_code in result.error_message  # policy 的稳定 reason code 直达用户
    assert result.data["reason"].startswith(expected_code)
    assert browser.new_page_calls == 0  # 拒绝路径上不建页、不导航
    assert browser.created == []


@pytest.mark.asyncio
async def test_browser_open_missing_url_is_blocked_by_policy():
    """url 缺失/非字符串同样走 policy 门（单一关口：reason 里带 invalid_url）。"""
    browser = FakeBrowser()
    ex = make_executor(browser)
    for arguments in ({}, {"url": None}, {"url": 123}):
        result = await ex.call("browser_open", arguments)
        assert result.status == "error"
        assert result.error_type == "navigation_blocked"
        assert "invalid_url" in result.error_message
    assert browser.new_page_calls == 0


@pytest.mark.asyncio
async def test_browser_open_uses_injected_allowlist_policy():
    """policy 可注入：allowlist 模式下白名单外的域名被拒、白名单内放行。"""
    browser = FakeBrowser()
    ex = make_executor(browser, policy=NavigationPolicy("allowlist", ["example.com"]))
    blocked = await ex.call("browser_open", {"url": "https://evil.com/"})
    assert blocked.error_type == "navigation_blocked"
    assert "domain_not_allowed" in blocked.error_message
    allowed = await ex.call("browser_open", {"url": "https://www.example.com/start"})
    assert allowed.status == "success"
    assert browser.created[0].goto_calls == ["https://www.example.com/start"]


@pytest.mark.asyncio
async def test_browser_open_policy_defaults_to_settings(monkeypatch):
    """不注入 policy 时由 Settings（BP_ 环境）构造：默认 allow_https 放行公网、挡内网。"""
    monkeypatch.setenv("BP_NAVIGATION_POLICY", "allow_https")
    browser = FakeBrowser()
    ex = make_executor(browser)
    blocked = await ex.call("browser_open", {"url": "http://192.168.1.1/"})
    assert blocked.error_type == "navigation_blocked"
    assert "private_address_blocked" in blocked.error_message
    assert (await ex.call("browser_open", {"url": "https://example.com/"})).status == "success"


# ── 当前页跟踪 ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browser_open_returns_navigation_data():
    browser = FakeBrowser()
    result = await make_executor(browser).call("browser_open", {"url": "https://example.com/page"})
    assert result.status == "success"
    assert result.data["requested_url"] == "https://example.com/page"
    assert result.data["url"] == "https://example.com/page"
    assert result.data["status"] == 200
    assert browser.created[0].goto_calls == ["https://example.com/page"]
    assert "已打开" in result.content


@pytest.mark.asyncio
async def test_browser_open_reuses_the_same_page_across_calls():
    browser = FakeBrowser()
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/a"})
    await ex.call("browser_open", {"url": "https://example.com/b"})
    assert browser.new_page_calls == 1  # 同一个 current page，不额外占池
    assert browser.created[0].goto_calls == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.asyncio
async def test_browser_open_failure_keeps_page_for_later_observation():
    """导航失败也是数据：current page 仍然可用（后续 snapshot 继续观察）。"""
    page = FakePage()
    page.fail_goto = True
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    failed = await ex.call("browser_open", {"url": "https://example.com/down"})
    assert failed.status == "error"
    assert failed.error_type == "navigation_failed"
    assert (await ex.call("browser_snapshot", {})).status == "success"


@pytest.mark.asyncio
async def test_browser_open_tolerates_networkidle_timeout():
    """有界等待超时只说明"页面还没静"，不是失败（T2 的 D3 语义）。"""
    page = FakePage()
    page.fail_networkidle_wait = True
    result = await make_executor(FakeBrowser(page)).call(
        "browser_open", {"url": "https://example.com/slow"}
    )
    assert result.status == "success"
    assert result.data["wait_timeout"] is True


@pytest.mark.asyncio
async def test_browser_pool_not_started_degrades_to_browser_unavailable():
    browser = FakeBrowser(start_error=RuntimeError("BrowserPool.new_page() called before start()"))
    result = await make_executor(browser).call("browser_open", {"url": "https://example.com/"})
    assert result.status == "error"
    assert result.error_type == "browser_unavailable"
    assert "start()" in result.error_message


@pytest.mark.asyncio
async def test_concurrent_opens_share_one_page():
    """并发 open 也不会各建一个 page（检查 current page 与建页之间没有互斥时会）。"""
    browser = FakeBrowser()
    ex = make_executor(browser)
    await asyncio.gather(
        ex.call("browser_open", {"url": "https://example.com/a"}),
        ex.call("browser_open", {"url": "https://example.com/b"}),
    )
    assert browser.new_page_calls == 1
    assert browser.created[0].goto_calls == ["https://example.com/a", "https://example.com/b"]


# ── snapshot ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_without_open_page_is_structured_error():
    browser = FakeBrowser()
    result = await make_executor(browser).call("browser_snapshot", {})
    assert result.status == "error"
    assert result.error_type == "no_open_page"
    assert "browser_open" in result.error_message
    assert browser.new_page_calls == 0  # 不偷偷建页：没有页面就是没有页面


@pytest.mark.asyncio
async def test_snapshot_returns_refs_for_click():
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.calls.clear()
    result = await ex.call("browser_snapshot", {})
    assert result.status == "success"
    assert "[ref=e2]" in result.content
    assert result.data["refs"] == result.content
    assert result.data["url"] == "https://example.com/"
    assert ("aria_snapshot", "body", "ai") in page.calls  # mode="ai" 是唯一产出 ref 的模式


@pytest.mark.asyncio
async def test_snapshot_empty_page_is_failure_so_chain_can_degrade():
    """空快照按失败处理：策略链会继续降级（否则把"什么都没有"当成功汇报）。"""
    page = FakePage(snapshot_tree="")
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_snapshot", {})
    assert result.status == "error"
    assert result.error_type == "empty_snapshot"


@pytest.mark.asyncio
async def test_snapshot_failure_is_structured():
    page = FakePage()
    page.fail_snapshot = True
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_snapshot", {})
    assert result.status == "error"
    assert result.error_type == "snapshot_failed"
    assert "aria snapshot unavailable" in result.error_message


# ── click / type / scroll（委托给 actions，错误原样结构化） ───────────────────


@pytest.mark.asyncio
async def test_click_uses_aria_ref_for_snapshot_labels():
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.calls.clear()
    result = await ex.call("browser_click", {"ref": "e2"})
    assert result.status == "success"
    assert ("click", "aria-ref=e2") in page.calls
    assert result.data["resolution"] == {"kind": "ref", "selector": "aria-ref=e2"}
    assert result.data["js_fallback"] is False


@pytest.mark.asyncio
async def test_click_accepts_target_alias_and_non_str_ref():
    """模型可能写 target/CSS/数字 ref：别名与强转都在这一层，actions 负责分级（T2 契约）。"""
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.calls.clear()
    text_hit = await ex.call("browser_click", {"target": "More information"})
    assert text_hit.status == "success"
    assert ("get_by_text", "More information", False) in page.calls
    numeric = await ex.call("browser_click", {"ref": 3})
    assert numeric.status == "success"
    assert ("click", "aria-ref=3") in page.calls


@pytest.mark.asyncio
async def test_click_missing_target_is_invalid_not_crash():
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_click", {})
    assert result.status == "error"
    assert result.error_type == "invalid_target"


@pytest.mark.asyncio
async def test_click_falls_back_to_js_and_reports_js_fallback():
    """Playwright 点击超时 → JS 兜底触发；js_fallback=True 意为"已派发，效果未证"。"""
    page = FakePage()
    page.fail_click = True
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_click", {"ref": "e2"})
    assert result.status == "success"
    assert result.data["js_fallback"] is True
    assert any(call[0] == "locator_evaluate" for call in page.calls)


@pytest.mark.asyncio
async def test_click_failure_keeps_attempts_and_errors_from_actions():
    page = FakePage()
    page.fail_click = True
    page.fail_locator_evaluate = True
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_click", {"ref": "e2"})
    assert result.status == "error"
    assert result.error_type == "timeout"
    assert result.data["attempts"] == 3  # 1 + ACTION_RETRIES(2)，ref 单候选
    assert result.data["target"] == "e2"
    assert result.data["errors"]  # 每次尝试的失败原因都保留给降级/诊断


@pytest.mark.asyncio
async def test_type_clears_before_filling_and_requires_text():
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.calls.clear()
    filled = await ex.call("browser_type", {"ref": "e2", "text": "蓝牙耳机"})
    assert filled.status == "success"
    assert [call for call in page.calls if call[0] == "fill"] == [
        ("fill", "aria-ref=e2", ""),
        ("fill", "aria-ref=e2", "蓝牙耳机"),
    ]
    missing = await ex.call("browser_type", {"ref": "e2"})
    assert missing.status == "error"
    assert missing.error_type == "invalid_text"


@pytest.mark.asyncio
async def test_scroll_defaults_to_down_and_honours_direction_amount():
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.calls.clear()
    default = await ex.call("browser_scroll", {})
    assert default.status == "success"
    assert ("wheel", 0, 500) in page.calls
    assert default.data["direction"] == "down"
    up = await ex.call("browser_scroll", {"direction": "up", "amount": 120})
    assert up.status == "success"
    assert ("wheel", 0, -120) in page.calls
    bad = await ex.call("browser_scroll", {"direction": "斜着"})
    assert bad.status == "error"
    assert bad.error_type == "invalid_direction"


# ── execute_js（只读 + 截断） ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression",
    [
        "document.title",
        "document.querySelectorAll('a').length",
        "JSON.stringify({u: location.href, t: document.title})",
        "document.body.innerText",
    ],
)
async def test_execute_js_allows_read_only_extraction(expression):
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_execute_js", {"expression": expression})
    assert result.status == "success"
    assert ("evaluate", expression) in page.calls
    assert result.data["result"] == "Example Domain"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression",
    [
        "fetch('/api/delete')",
        "document.cookie = 'k=v'",
        "document.body.innerHTML = '<b>x</b>'",
        "localStorage.setItem('k', 'v')",
        "location.href = 'https://evil.example/'",
        "window.open('https://evil.example/')",
        "document.querySelector('form').submit()",
        "document.querySelector('button').click()",
        "eval('1+1')",
        "new Function('return 1')()",
        "navigator.clipboard.writeText('x')",
    ],
)
async def test_execute_js_blocks_write_expressions(expression):
    """D1「execute_js 仅读」落地：写操作静态拒（best-effort 黑名单，见 executor docstring）。"""
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.calls.clear()
    result = await ex.call("browser_execute_js", {"expression": expression})
    assert result.status == "error"
    assert result.error_type == "read_only_violation"
    assert not [call for call in page.calls if call[0] == "evaluate"]  # 拒绝路径不执行


@pytest.mark.asyncio
async def test_execute_js_rejects_missing_or_non_str_expression():
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.calls.clear()
    for arguments in ({}, {"expression": None}, {"expression": 42}, {"expression": "   "}):
        result = await ex.call("browser_execute_js", arguments)
        assert result.status == "error"
        assert result.error_type == "invalid_expression"
    assert not [call for call in page.calls if call[0] == "evaluate"]


@pytest.mark.asyncio
async def test_execute_js_truncates_before_state():
    page = FakePage(js_result="x" * (MAX_JS_RESULT_CHARS + 50))
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_execute_js", {"expression": "document.body.innerText"})
    assert result.status == "success"
    assert len(result.content) == MAX_JS_RESULT_CHARS
    assert result.data["result"] == result.content
    assert result.data["truncated"] is True


@pytest.mark.asyncio
async def test_execute_js_serialises_non_string_results():
    page = FakePage(js_result={"title": "Example", "links": 3})
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_execute_js", {"expression": "(() => ({title: document.title}))()"})
    assert result.status == "success"
    assert json.loads(result.content) == {"title": "Example", "links": 3}
    assert result.data["truncated"] is False


@pytest.mark.asyncio
async def test_execute_js_failure_is_structured():
    page = FakePage()
    page.fail_evaluate = True
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_execute_js", {"expression": "document.title"})
    assert result.status == "error"
    assert result.error_type == "evaluate_failed"
    assert "ReferenceError" in result.error_message


@pytest.mark.asyncio
async def test_browser_call_with_broken_arguments_never_raises():
    """守护"失败即数据"：即使入参形状超纲（None），也要结构化错误而不是异常冒泡。"""
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_click", None)  # type: ignore[arg-type]
    assert result.status == "error"
    assert result.error_type == "browser_error"
