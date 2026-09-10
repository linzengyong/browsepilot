"""Tier3 动作层单测：多重回退定位（ref → CSS → 文本）+ click/type/scroll/navigate 原语。

不启浏览器：fake page/locator 双人舞（延续 T1 的测试缝风格）——
page 记录 ``locator()`` / ``get_by_text()`` 的解析顺序与每个动作的尝试次数，
locator 用"剩余失败次数"模拟 Playwright 的自动等待超时（含 TimeoutError 形态）。
真浏览器冒烟是独立脚本，不进 pytest（教训 §1）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from browsepilot.agent.tools import ToolResult
from browsepilot.app.browser.actions import (
    ACTION_RETRIES,
    ACTION_TIMEOUT_MS,
    DEFAULT_SCROLL_PX,
    DEFAULT_WAIT_MS,
    JS_CLICK,
    MAX_RETRIES,
    NAVIGATE_TIMEOUT_MS,
    Resolution,
    classify_target,
    click,
    navigate,
    resolve_locator,
    scroll,
    scroll_into_view,
    type_text,
)

# ── fake page / locator（动作层的测试缝） ──────────────────────────────────────


class _FakeMouse:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        self._page._fail_if("wheel")
        self._page.wheel_calls.append((delta_x, delta_y))


class _FakeLocator:
    """只实现动作层用到的 locator 方法；失败由所属 page 的剩余失败次数驱动。"""

    def __init__(self, selector: str, page: _FakePage) -> None:
        self.selector = selector
        self._page = page

    async def click(self, *, timeout: float | None = None) -> None:
        self._page.click_attempts.append((self.selector, timeout))
        self._page._fail_if("click")

    async def fill(self, value: str, *, timeout: float | None = None) -> None:
        self._page.fill_attempts.append((self.selector, value))
        self._page._fail_if("fill")
        self._page.value = value  # fill 是替换语义（与 Playwright 一致）

    async def evaluate(
        self, expression: str, arg: Any = None, *, timeout: float | None = None
    ) -> Any:
        self._page.evaluate_calls.append(
            {"selector": self.selector, "expression": expression, "arg": arg, "timeout": timeout}
        )
        self._page._fail_if("evaluate")
        return True

    async def scroll_into_view_if_needed(self, *, timeout: float | None = None) -> None:
        self._page.scroll_into_view_attempts.append((self.selector, timeout))
        self._page._fail_if("scroll_into_view")


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status


class _FakePage:
    """记录解析顺序 / 尝试次数，并可编程失败（``fail("click", times=2)`` 前两次点击失败）。"""

    def __init__(self, *, url: str = "https://example.com/", goto_url: str | None = None) -> None:
        self.url = url
        self._goto_url = goto_url
        self.locator_calls: list[str] = []
        self.text_calls: list[tuple[str, bool | None]] = []
        self.click_attempts: list[tuple[str, float | None]] = []
        self.fill_attempts: list[tuple[str, str]] = []
        self.evaluate_calls: list[dict[str, Any]] = []
        self.scroll_into_view_attempts: list[tuple[str, float | None]] = []
        self.wheel_calls: list[tuple[float, float]] = []
        self.goto_calls: list[tuple[str, str | None, float | None]] = []
        self.wait_calls: list[tuple[str | None, float | None]] = []
        self.value = ""
        self.mouse = _FakeMouse(self)
        self._failures: dict[str, int] = defaultdict(int)
        self._fail_exc: dict[str, BaseException] = {}

    def fail(self, kind: str, times: int = 1, exc: BaseException | None = None) -> _FakePage:
        self._failures[kind] = times
        if exc is not None:
            self._fail_exc[kind] = exc
        return self

    def _fail_if(self, kind: str) -> None:
        if self._failures[kind] > 0:
            self._failures[kind] -= 1
            raise self._fail_exc.get(kind) or RuntimeError(f"fake {kind} failure")

    def locator(self, selector: str, **_kwargs: Any) -> _FakeLocator:
        self.locator_calls.append(selector)
        return _FakeLocator(selector, self)

    def get_by_text(self, text: str, *, exact: bool | None = None) -> _FakeLocator:
        self.text_calls.append((text, exact))
        return _FakeLocator(f"text={text}", self)

    async def goto(
        self, url: str, *, wait_until: str | None = None, timeout: float | None = None
    ) -> _FakeResponse:
        self.goto_calls.append((url, wait_until, timeout))
        self._fail_if("goto")
        self.url = self._goto_url or url
        return _FakeResponse()

    async def wait_for_load_state(
        self, state: str | None = None, *, timeout: float | None = None
    ) -> None:
        self.wait_calls.append((state, timeout))
        self._fail_if("wait")


# ── classify_target：ref（最高优先）→ CSS → 文本（最后手段） ──────────────────


@pytest.mark.parametrize(
    ("target", "kind", "selector"),
    [
        # ref 的各种写法（T1 的 PageSnapshot.label 直接可用）
        ("e2", "ref", "aria-ref=e2"),
        ("@e2", "ref", "aria-ref=e2"),
        ("aria-ref=e2", "ref", "aria-ref=e2"),
        ("ref=e2", "ref", "aria-ref=e2"),
        ("[ref=e7]", "ref", "aria-ref=e7"),
        ("E2", "ref", "aria-ref=e2"),  # label 归一化（Playwright 的 ref 总是小写 eN）
        ("3", "ref", "aria-ref=3"),  # 块式 YAML 的纯数字 label
        ("#3", "ref", "aria-ref=3"),  # 数字 id 不是合法 CSS，模型也可能写成 #N
        # CSS
        ("#search", "css", "#search"),
        (".btn", "css", ".btn"),
        ("[data-testid=q]", "css", "[data-testid=q]"),
        ("input", "css", "input"),
        ("div button", "css", "div button"),
        ("button[type=submit]", "css", "button[type=submit]"),
        ("div#main .item", "css", "div#main .item"),
        # 文本（最后手段）
        ("Sign in", "text", "Sign in"),
        ("Sign in button", "text", "Sign in button"),
        ("搜索", "text", "搜索"),
        ("Video", "text", "Video"),  # 大小写敏感：只有全小写 tag 才按 CSS
        ("Note: hello", "text", "Note: hello"),  # 自然语言里的冒号不是伪类
    ],
)
def test_classify_target_ordered_fallbacks(target: str, kind: str, selector: str) -> None:
    assert classify_target(target) == Resolution(kind, selector)


async def test_resolve_locator_prefers_ref():
    page = _FakePage()

    locator = resolve_locator(page, "e2")

    assert page.locator_calls == ["aria-ref=e2"]  # 原生 ref 选择器，不靠索引推算
    assert page.text_calls == []
    assert locator.selector == "aria-ref=e2"


async def test_resolve_locator_uses_css_when_selector_shaped():
    page = _FakePage()

    resolve_locator(page, "#search")

    assert page.locator_calls == ["#search"]
    assert page.text_calls == []


async def test_resolve_locator_falls_back_to_text_for_plain_words():
    page = _FakePage()

    resolve_locator(page, "Sign in")

    assert page.text_calls == [("Sign in", False)]  # exact=False：模糊匹配
    assert page.locator_calls == []


async def test_resolve_locator_empty_target_raises():
    page = _FakePage()

    with pytest.raises(ValueError):
        resolve_locator(page, "   ")

    assert page.locator_calls == [] and page.text_calls == []


async def test_resolve_locator_records_resolution_path():
    page = _FakePage()
    trace: list[Resolution] = []

    resolve_locator(page, "e7", trace=trace)

    assert [(r.kind, r.selector, str(r)) for r in trace] == [
        ("ref", "aria-ref=e7", "ref:aria-ref=e7")
    ]


# ── click：重试（每次重新 resolve）→ JS 触发兜底 ──────────────────────────────


async def test_click_ref_success_reports_resolution_and_attempts():
    page = _FakePage()

    result = await click(page, "e2")

    assert result["status"] == "success"
    assert result["content"]  # 人类可读文案
    assert result["error_type"] == "" and result["error_message"] == ""
    assert page.click_attempts == [("aria-ref=e2", ACTION_TIMEOUT_MS)]
    assert result["data"]["attempts"] == 1
    assert result["data"]["js_fallback"] is False
    assert result["data"]["target"] == "e2"
    assert result["data"]["resolution"] == {"kind": "ref", "selector": "aria-ref=e2"}
    assert result["data"]["url"] == "https://example.com/"
    assert result["data"]["action"] == "click"


async def test_click_retries_with_fresh_resolve_then_succeeds():
    page = _FakePage().fail("click", 1)

    result = await click(page, "#go", retries=2)

    assert result["status"] == "success"
    assert page.locator_calls == ["#go", "#go"]  # 每次尝试都重新 resolve
    assert len(page.click_attempts) == 2
    assert result["data"]["attempts"] == 2
    assert result["data"]["js_fallback"] is False
    assert page.evaluate_calls == []  # 没用 JS 兜底


async def test_click_falls_back_to_js_trigger_after_retries():
    page = _FakePage().fail("click", 99)

    result = await click(page, "e2", retries=2)

    assert result["status"] == "success"
    assert len(page.click_attempts) == 1 + 2  # 初次 + retries
    assert JS_CLICK == "el => el.click()"  # brief 指定的兜底表达式
    assert page.evaluate_calls == [
        {
            "selector": "aria-ref=e2",
            "expression": "el => el.click()",
            "arg": None,
            "timeout": ACTION_TIMEOUT_MS,
        }
    ]
    assert result["data"]["js_fallback"] is True
    assert result["data"]["attempts"] == 3


async def test_click_retries_zero_means_single_attempt():
    page = _FakePage().fail("click", 1)

    result = await click(page, "#go", retries=0)

    assert len(page.click_attempts) == 1
    assert len(page.evaluate_calls) == 1
    assert result["status"] == "success"
    assert result["data"]["attempts"] == 1


async def test_click_returns_error_dict_when_js_fallback_also_fails():
    page = _FakePage().fail("click", 99, exc=PlaywrightTimeoutError("click timed out")).fail(
        "evaluate", 99, exc=PlaywrightTimeoutError("js click timed out")
    )

    result = await click(page, "e2")

    assert result["status"] == "error"
    assert result["error_type"] == "timeout"
    assert "e2" in result["error_message"]
    assert result["data"]["attempts"] == 1 + ACTION_RETRIES
    assert result["data"]["js_fallback"] is True
    assert len(result["data"]["errors"]) == 1 + ACTION_RETRIES + 1  # 3 次动作 + 1 次 JS


async def test_click_returns_error_dict_on_generic_failure():
    page = _FakePage().fail("click", 99).fail("evaluate", 99)

    result = await click(page, "#go")

    assert result["status"] == "error"
    assert result["error_type"] == "action_failed"
    assert "fake evaluate failure" in result["error_message"]


async def test_click_blank_target_is_error_dict_not_raise():
    page = _FakePage()

    result = await click(page, "   ")

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_target"
    assert page.click_attempts == [] and page.evaluate_calls == []


async def test_click_clamps_out_of_range_retries():
    page = _FakePage().fail("click", 99)

    result = await click(page, "#go", retries=10_000)

    assert len(page.click_attempts) == 1 + MAX_RETRIES  # 上限收敛，不无限重试
    assert result["status"] == "success"


# ── type_text：先清空再填（P3-D7 幂等），重试 → JS 赋值兜底 ────────────────────


async def test_type_clears_before_filling():
    page = _FakePage()

    result = await type_text(page, "#q", "hello")

    assert result["status"] == "success"
    assert page.fill_attempts == [("#q", ""), ("#q", "hello")]  # 先清空，再填
    assert result["data"]["text"] == "hello"
    assert result["data"]["resolution"] == {"kind": "css", "selector": "#q"}


async def test_type_replay_is_idempotent():
    page = _FakePage()

    await type_text(page, "#q", "hello")
    await type_text(page, "#q", "hello")  # resume 重放同一个动作

    assert page.value == "hello"  # 不是 "hellohello"：fill 替换语义 + 先清空
    assert page.fill_attempts == [("#q", ""), ("#q", "hello")] * 2


async def test_type_retries_with_fresh_resolve_and_reclears():
    page = _FakePage().fail("fill", 1)

    result = await type_text(page, "e9", "hi", retries=2)

    assert result["status"] == "success"
    assert page.locator_calls == ["aria-ref=e9", "aria-ref=e9"]
    assert page.fill_attempts == [  # 第一次尝试清空即失败，重试重新走"清空 → 填"
        ("aria-ref=e9", ""),
        ("aria-ref=e9", ""),
        ("aria-ref=e9", "hi"),
    ]
    assert result["data"]["attempts"] == 2
    assert page.evaluate_calls == []


async def test_type_falls_back_to_js_value_set():
    page = _FakePage().fail("fill", 99)

    result = await type_text(page, "#q", "hello", retries=1)

    assert result["status"] == "success"
    assert len(page.fill_attempts) == 2  # 1 次 + 1 次重试，全部失败
    assert len(page.evaluate_calls) == 1
    call = page.evaluate_calls[0]
    assert call["selector"] == "#q"
    assert call["arg"] == "hello"  # 赋值语义（幂等），不是追加
    assert "value" in call["expression"] and "input" in call["expression"]
    assert result["data"]["js_fallback"] is True


async def test_type_returns_error_dict_when_everything_fails():
    page = _FakePage().fail("fill", 99, exc=PlaywrightTimeoutError("fill timed out")).fail(
        "evaluate", 99
    )

    result = await type_text(page, "#q", "hi")

    assert result["status"] == "error"
    assert result["error_type"] == "timeout"
    assert "#q" in result["error_message"]
    assert result["data"]["js_fallback"] is True


async def test_type_blank_target_is_error_dict():
    page = _FakePage()

    result = await type_text(page, "", "hi")

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_target"
    assert page.fill_attempts == [] and page.evaluate_calls == []


# ── scroll / scroll_into_view ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("direction", "expected"),
    [("down", (0, DEFAULT_SCROLL_PX)), ("up", (0, -DEFAULT_SCROLL_PX)), ("right", (DEFAULT_SCROLL_PX, 0)), ("left", (-DEFAULT_SCROLL_PX, 0)), ("DOWN", (0, DEFAULT_SCROLL_PX))],
)
async def test_scroll_maps_direction_to_wheel(direction: str, expected: tuple[int, int]) -> None:
    page = _FakePage()

    result = await scroll(page, direction)

    assert page.wheel_calls == [expected]
    assert result["status"] == "success"
    assert result["data"]["amount"] == DEFAULT_SCROLL_PX
    assert result["data"]["direction"] == direction.lower()


async def test_scroll_custom_amount():
    page = _FakePage()

    await scroll(page, "down", 250)

    assert page.wheel_calls == [(0, 250)]


async def test_scroll_negative_amount_uses_magnitude():
    page = _FakePage()

    await scroll(page, "up", -250)

    assert page.wheel_calls == [(0, -250)]  # 方向定符号，amount 只取大小


async def test_scroll_rejects_unknown_direction():
    page = _FakePage()

    result = await scroll(page, "sideways")

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_direction"
    assert page.wheel_calls == []


async def test_scroll_rejects_non_numeric_amount():
    page = _FakePage()

    result = await scroll(page, "down", "lots")  # type: ignore[arg-type]

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_amount"
    assert page.wheel_calls == []


async def test_scroll_wheel_failure_is_error_dict():
    page = _FakePage().fail("wheel", 1)

    result = await scroll(page, "down")

    assert result["status"] == "error"
    assert result["error_type"] == "action_failed"
    assert "fake wheel failure" in result["error_message"]


async def test_scroll_into_view_resolves_target():
    page = _FakePage()

    result = await scroll_into_view(page, "e5")

    assert page.scroll_into_view_attempts == [("aria-ref=e5", ACTION_TIMEOUT_MS)]
    assert result["status"] == "success"
    assert result["data"]["resolution"] == {"kind": "ref", "selector": "aria-ref=e5"}


async def test_scroll_into_view_error_dict():
    page = _FakePage().fail("scroll_into_view", 99, exc=PlaywrightTimeoutError("not found"))

    result = await scroll_into_view(page, "#x", retries=1)

    assert result["status"] == "error"
    assert result["error_type"] == "timeout"
    assert len(page.scroll_into_view_attempts) == 2  # 1 + retries


# ── navigate：domcontentloaded + 有界条件等待（D3） ───────────────────────────


async def test_navigate_uses_domcontentloaded_then_bounded_wait():
    page = _FakePage(goto_url="https://example.com/final")

    result = await navigate(page, "https://example.com")

    assert page.goto_calls == [("https://example.com", "domcontentloaded", NAVIGATE_TIMEOUT_MS)]
    assert page.wait_calls == [("networkidle", DEFAULT_WAIT_MS)]
    assert result["status"] == "success"
    assert result["data"]["requested_url"] == "https://example.com"
    assert result["data"]["url"] == "https://example.com/final"  # 跟随重定向后的 URL
    assert result["data"]["status"] == 200
    assert result["data"]["waited"] is True
    assert result["data"]["wait_timeout"] is False


async def test_navigate_networkidle_timeout_is_not_fatal():
    page = _FakePage().fail("wait", 1, exc=PlaywrightTimeoutError("networkidle timed out"))

    result = await navigate(page, "https://slow.example")

    assert result["status"] == "success"  # 有界等待超时 ≠ 失败（D3）
    assert result["data"]["waited"] is False
    assert result["data"]["wait_timeout"] is True
    assert result["data"]["url"] == "https://slow.example"
    assert "500" in result["content"]  # 告知"没等静就继续"


async def test_navigate_wait_ms_zero_skips_condition_wait():
    page = _FakePage()

    result = await navigate(page, "https://example.com", wait_ms=0)

    assert page.wait_calls == []
    assert result["status"] == "success"
    assert result["data"]["waited"] is False


async def test_navigate_goto_timeout_is_error_dict():
    page = _FakePage().fail("goto", 1, exc=PlaywrightTimeoutError("nav timeout"))

    result = await navigate(page, "https://slow.example")

    assert result["status"] == "error"
    assert result["error_type"] == "timeout"
    assert page.wait_calls == []  # 导航失败 → 不再等待


async def test_navigate_generic_goto_failure_is_error_dict():
    page = _FakePage().fail("goto", 1, exc=RuntimeError("net::ERR_NAME_NOT_RESOLVED"))

    result = await navigate(page, "https://nope.invalid")

    assert result["status"] == "error"
    assert result["error_type"] == "navigation_failed"
    assert "ERR_NAME_NOT_RESOLVED" in result["error_message"]


async def test_navigate_blank_url_is_error_dict():
    page = _FakePage()

    result = await navigate(page, "   ")

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_url"
    assert page.goto_calls == []


# ── 结果契约（T4 的 executor 直接把它们转成 ToolResult） ─────────────────────


def test_action_retries_constant():
    assert ACTION_RETRIES == 2


async def test_result_dicts_match_toolresult_fields():
    ok_page = _FakePage()
    results = [
        await click(ok_page, "e2"),
        await type_text(ok_page, "e2", "hi"),
        await scroll(ok_page, "down"),
        await navigate(ok_page, "https://example.com"),
        await click(_FakePage(), " "),  # error 形态
        await navigate(_FakePage().fail("goto", 1), "https://nope.invalid"),
    ]

    for result in results:
        assert set(result) == {"status", "content", "data", "error_type", "error_message"}
        model = ToolResult(**result)
        assert model.status == result["status"]
        assert model.data == result["data"]
