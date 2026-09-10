"""Tier3 动作层单测：多重回退定位（ref → CSS → 文本）+ click/type/scroll/navigate 原语。

不启浏览器：fake page/locator 双人舞（延续 T1 的测试缝风格）——
page 记录 ``locator()`` / ``get_by_text()`` 的解析顺序与每个动作的尝试次数（``events`` 保留
跨方法的时间顺序，用来 pin "JS 兜底严格排在所有候选尝试之后"），
locator 用"剩余失败次数"模拟 Playwright 的自动等待超时（含 TimeoutError 形态），
失败可按 selector 注入（模拟"CSS 选择器非法/匹配不到，但同名文本存在"的行为回退场景）。
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
    build_candidates,
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
        self._page.events.append(f"wheel:{delta_x},{delta_y}")


class _FakeLocator:
    """只实现动作层用到的 locator 方法；失败由所属 page 的剩余失败次数驱动。"""

    def __init__(self, selector: str, page: _FakePage) -> None:
        self.selector = selector
        self._page = page

    async def click(self, *, timeout: float | None = None) -> None:
        self._page.click_attempts.append((self.selector, timeout))
        self._page.events.append(f"click:{self.selector}")
        self._page._fail_if("click", self.selector)

    async def fill(self, value: str, *, timeout: float | None = None) -> None:
        self._page.fill_attempts.append((self.selector, value))
        self._page.events.append(f"fill:{self.selector}={value!r}")
        self._page._fail_if("fill", self.selector)
        self._page.value = value  # fill 是替换语义（与 Playwright 一致）

    async def evaluate(
        self, expression: str, arg: Any = None, *, timeout: float | None = None
    ) -> Any:
        self._page.evaluate_calls.append(
            {"selector": self.selector, "expression": expression, "arg": arg, "timeout": timeout}
        )
        self._page.events.append(f"evaluate:{self.selector}")
        self._page._fail_if("evaluate", self.selector)
        return True

    async def scroll_into_view_if_needed(self, *, timeout: float | None = None) -> None:
        self._page.scroll_into_view_attempts.append((self.selector, timeout))
        self._page.events.append(f"scroll_into_view:{self.selector}")
        self._page._fail_if("scroll_into_view", self.selector)


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
        self.events: list[str] = []
        self.value = ""
        self.mouse = _FakeMouse(self)
        self._failures: dict[str, int] = defaultdict(int)
        self._fail_exc: dict[str, BaseException] = {}
        self._selector_failures: dict[tuple[str, str], int] = {}
        self._selector_fail_exc: dict[tuple[str, str], BaseException] = {}

    def fail(
        self,
        kind: str,
        times: int = 1,
        exc: BaseException | None = None,
        selector: str | None = None,
    ) -> _FakePage:
        """注入失败：``selector=None`` 对整个 kind 生效；给了 selector 则只对命中它的 locator 生效。"""
        if selector is None:
            self._failures[kind] = times
            if exc is not None:
                self._fail_exc[kind] = exc
        else:
            key = (kind, selector)
            self._selector_failures[key] = times
            if exc is not None:
                self._selector_fail_exc[key] = exc
        return self

    def _fail_if(self, kind: str, selector: str | None = None) -> None:
        if selector is not None:
            key = (kind, selector)
            if self._selector_failures.get(key, 0) > 0:
                self._selector_failures[key] -= 1
                raise self._selector_fail_exc.get(key) or RuntimeError(
                    f"fake {kind} failure for {selector!r}"
                )
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


# ── build_candidates：候选序列（F2 行为回退的根据） ────────────────────────


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("e2", [("ref", "aria-ref=e2")]),  # ref 无本地等价物：单候选（恢复靠重新快照）
        ("3", [("ref", "aria-ref=3")]),
        ("#search", [("css", "#search"), ("text", "#search")]),
        ("search", [("css", "search"), ("text", "search")]),  # HTML tag 同名 → 文本兜底
        ("Save:", [("css", "Save:"), ("text", "Save:")]),  # 冒号被当伪类 → 文本兜底
        ("Sign in", [("text", "Sign in")]),  # 文本已是最后一级：没有更弱的候选
    ],
)
def test_build_candidates_ordered_and_capped(
    target: str, expected: list[tuple[str, str]]
) -> None:
    candidates = build_candidates(target)

    assert [(r.kind, r.selector) for r in candidates] == expected
    assert len(candidates) <= 2  # 候选数有界：分类级最多 2 个候选


@pytest.mark.parametrize("target", ["", "   ", None])
def test_build_candidates_blank_raises(target: Any) -> None:
    with pytest.raises(ValueError):
        build_candidates(target)  # type: ignore[arg-type]


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


async def test_click_retries_round_with_fresh_resolve_then_succeeds():
    page = _FakePage().fail("click", 2)

    result = await click(page, "#go", retries=2)

    assert result["status"] == "success"
    # 第 1 轮：CSS 候选（#go）失败 → 文本候选失败；第 2 轮重新 resolve → CSS 候选命中
    assert page.locator_calls == ["#go", "#go"]
    assert page.text_calls == [("#go", False)]
    assert len(page.click_attempts) == 3
    assert result["data"]["attempts"] == 3
    assert result["data"]["resolution"] == {"kind": "css", "selector": "#go"}
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


async def test_click_retries_zero_walks_candidates_within_one_round():
    page = _FakePage().fail("click", 1)

    result = await click(page, "#go", retries=0)

    # retries=0 → 只有一轮；轮内候选仍走完（CSS 失败 → 文本命中），不是"只试一次"
    assert [selector for selector, _ in page.click_attempts] == ["#go", "text=#go"]
    assert len(page.evaluate_calls) == 0
    assert result["status"] == "success"
    assert result["data"]["attempts"] == 2
    assert result["data"]["resolution"] == {"kind": "text", "selector": "#go"}


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

    # 轮数收敛到 1 + MAX_RETRIES；每轮走完 2 个候选 → 上限 (1 + MAX_RETRIES) * 2
    assert len(page.click_attempts) == (1 + MAX_RETRIES) * 2
    assert result["status"] == "success"


async def test_click_falls_through_to_text_candidate_when_css_is_not_a_selector():
    # "Save:" 被 _looks_like_css 当成伪类选择器（非法 CSS）；文本候选必须兜住（F2）
    page = _FakePage().fail(
        "click",
        99,
        exc=RuntimeError("Unexpected token ':' while parsing selector"),
        selector="Save:",
    )

    result = await click(page, "Save:", retries=2)

    assert result["status"] == "success"
    assert result["data"]["resolution"] == {"kind": "text", "selector": "Save:"}  # 命中文本候选
    assert result["data"]["js_fallback"] is False
    assert result["data"]["attempts"] == 2  # CSS 候选失败 1 次 → 立即试文本候选
    assert [selector for selector, _ in page.click_attempts] == ["Save:", "text=Save:"]
    assert page.text_calls == [("Save:", False)]
    assert len(result["data"]["errors"]) == 1  # 只记录了 CSS 候选那次失败
    assert "Save:" in result["data"]["errors"][0]


async def test_ref_target_never_probes_other_candidates():
    """ref 没有本地等价物：失败只能交给 agent 层重新快照，绝不盲试 CSS/文本。"""
    page = _FakePage().fail("click", 99).fail("evaluate", 99)

    result = await click(page, "e2", retries=1)

    assert result["status"] == "error"
    # 2 次动作尝试 + 1 次 JS 兜底：全部只 resolve ref，没换过候选
    assert page.locator_calls == ["aria-ref=e2"] * 3
    assert page.text_calls == []  # 没有文本候选
    assert [call["selector"] for call in page.evaluate_calls] == ["aria-ref=e2"]  # JS 也不换候选


async def test_js_fallback_runs_strictly_after_every_candidate_attempt():
    page = _FakePage().fail("click", 99).fail("evaluate", 99, selector="#go")

    result = await click(page, "#go", retries=0)

    # 事件顺序：先走完候选尝试，再按同一候选序走 JS 兜底（严格排最后）
    assert page.events == ["click:#go", "click:text=#go", "evaluate:#go", "evaluate:text=#go"]
    assert result["status"] == "success"
    assert result["data"]["js_fallback"] is True
    assert result["data"]["resolution"] == {"kind": "text", "selector": "#go"}  # JS 命中文本候选


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
    assert len(page.fill_attempts) == 4  # 2 候选 × (1 + retries) 次尝试，全部在"清空"这步失败
    assert len(page.evaluate_calls) == 1
    call = page.evaluate_calls[0]
    assert call["selector"] == "#q"
    assert call["arg"] == "hello"  # 赋值语义（幂等），不是追加
    assert "value" in call["expression"] and "input" in call["expression"]
    assert result["data"]["js_fallback"] is True


async def test_type_falls_through_to_text_candidate():
    page = _FakePage().fail("fill", 99, selector="Save:")

    result = await type_text(page, "Save:", "hi", retries=1)

    assert result["status"] == "success"
    assert result["data"]["resolution"] == {"kind": "text", "selector": "Save:"}
    assert page.fill_attempts == [("Save:", ""), ("text=Save:", ""), ("text=Save:", "hi")]


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


@pytest.mark.parametrize("amount", ["lots", {}, [1], {"px": 300}])
async def test_scroll_rejects_non_numeric_amount(amount: Any) -> None:
    page = _FakePage()

    result = await scroll(page, "down", amount)  # type: ignore[arg-type]

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
    assert len(page.scroll_into_view_attempts) == 4  # 2 候选 × (1 + retries)


async def test_scroll_into_view_falls_through_to_text_candidate():
    page = _FakePage().fail("scroll_into_view", 99, selector="#x")

    result = await scroll_into_view(page, "#x", retries=1)

    assert result["status"] == "success"
    assert result["data"]["resolution"] == {"kind": "text", "selector": "#x"}
    assert page.scroll_into_view_attempts == [
        ("#x", ACTION_TIMEOUT_MS),
        ("text=#x", ACTION_TIMEOUT_MS),
    ]


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


@pytest.mark.parametrize("url", [None, 5, True, {"url": "https://x"}, ["https://x"]])
async def test_navigate_non_str_url_is_invalid_url(url: Any) -> None:
    page = _FakePage()

    result = await navigate(page, url)  # type: ignore[arg-type]

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_url"
    assert page.goto_calls == []  # 不把垃圾当相对路径真的导航出去（F1）


# ── 入参收敛（F1：executor 直传 dict[str, Any]，非 str 入参绝不 AttributeError） ──


def test_classify_target_coerces_non_str_target():
    # 分级本身也得收敛：它是 __all__ 里的公共面（T4 可直接调），不是只给内部用的
    assert classify_target(3) == Resolution("ref", "aria-ref=3")  # type: ignore[arg-type]
    assert classify_target(True) == Resolution("text", "True")  # type: ignore[arg-type]
    # 小数点让它看起来像 CSS（.class 起手）；万一选择器非法还有候选序列里的文本兜底
    assert classify_target(3.5) == Resolution("css", "3.5")  # type: ignore[arg-type]


async def test_resolve_locator_coerces_non_str_target():
    page = _FakePage()

    locator = resolve_locator(page, 3)  # type: ignore[arg-type]

    assert page.locator_calls == ["aria-ref=3"]
    assert locator.selector == "aria-ref=3"


async def test_click_numeric_target_is_coerced_to_ref_label():
    page = _FakePage()

    result = await click(page, 3)  # type: ignore[arg-type]

    assert result["status"] == "success"
    assert page.locator_calls == ["aria-ref=3"]  # JSON 数字 label 是受支持的 ref 写法（3 → "3"）
    assert page.text_calls == []
    assert result["data"]["target"] == "3"  # 进状态的是收敛后的 str
    assert result["data"]["resolution"] == {"kind": "ref", "selector": "aria-ref=3"}


async def test_type_numeric_target_is_coerced_to_ref_label():
    page = _FakePage()

    result = await type_text(page, 3, "hi")  # type: ignore[arg-type]

    assert result["status"] == "success"
    assert page.fill_attempts == [("aria-ref=3", ""), ("aria-ref=3", "hi")]


@pytest.mark.parametrize("text", [None, 5, ["hi"], {"v": "hi"}])
async def test_type_non_str_text_is_invalid_text_never_js_null(text: Any) -> None:
    """None 不能落到 JS 兜底被赋成字面量 "null"；非 str 一律结构化拒绝（F1）。"""
    page = _FakePage()

    result = await type_text(page, "#q", text)  # type: ignore[arg-type]

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_text"
    assert page.fill_attempts == [] and page.evaluate_calls == []  # 一个页面调用都没发生
    assert result["data"]["target"] == "#q"


async def test_scroll_numeric_direction_is_invalid_direction():
    page = _FakePage()

    result = await scroll(page, 5)  # type: ignore[arg-type]

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_direction"
    assert page.wheel_calls == []


@pytest.mark.parametrize("garbage", [3, 3.5, True, None, {"ref": 3}, ["e2"]])
async def test_actions_never_raise_on_non_str_arguments(garbage: Any) -> None:
    """模块契约：任何入参都转成结构化 dict，绝不向图抛异常（F1 回归网）。"""
    page = _FakePage()

    results = [
        await click(page, garbage),  # type: ignore[arg-type]
        await type_text(page, garbage, "hi"),  # type: ignore[arg-type]
        await scroll(page, garbage),  # type: ignore[arg-type]
        await scroll_into_view(page, garbage),  # type: ignore[arg-type]
        await navigate(page, garbage),  # type: ignore[arg-type]
    ]

    for result in results:
        assert set(result) == {"status", "content", "data", "error_type", "error_message"}
        assert result["status"] in {"success", "error"}
    assert results[2]["error_type"] == "invalid_direction"  # scroll 的 direction 错型 → 结构化拒绝
    assert results[4]["error_type"] == "invalid_url"  # navigate 的 url 错型 → 结构化拒绝


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
