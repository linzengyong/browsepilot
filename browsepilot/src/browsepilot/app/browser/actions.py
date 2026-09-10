"""Tier3 动作原语：多重回退定位（ref → CSS → 文本）+ click / type / scroll / navigate。

本模块属于 app 层：只依赖 playwright 与标准库，绝不 import ``browsepilot.agent.*``
（结果用 ToolResult 兼容的 dict 交出，转换在 executor 里做）。

设计要点（P3-D2 / D3 / D7）：

- **多重回退定位**（:func:`resolve_locator`）：ref（Playwright 原生 ``aria-ref=eN``，
  T1 快照产出的 ``PageSnapshot.label`` 直接可用）→ CSS（``#id`` / ``.class`` / ``[attr]``
  / 已知 HTML tag）→ 文本（``get_by_text(exact=False)``，最后手段）。
  分级是**纯字符串判定**（同步、零往返）：命中哪一级由 target 的写法决定，
  "页面里到底有没有这个元素"由动作重试与 JS 兜底负责。
- **重试**：click / type 每次尝试都**重新 resolve**（页面可能已变），共 ``1 + retries`` 次；
  仍失败 → 最后手段 JS（click：``el => el.click()``；type：赋值 + input/change 事件）。
- **幂等**（P3-D7，resume 重放安全）：type 先 ``fill("")`` 再 ``fill(text)``
  （清空重填，绝不追加）；JS 兜底同样是赋值语义。故重放同一动作结果一致。
- **导航语义**（D3）：``goto(wait_until="domcontentloaded")`` 之后做**有界**条件等待
  （``networkidle``，默认 500ms）；等待超时只说明"页面还没静"，绝不当作失败。
- **失败即数据**：任何异常都转成 ``status="error"`` 的结构化 dict
  （``content`` / ``data`` / ``error_type`` / ``error_message``），绝不向图抛异常。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

__all__ = [
    "ACTION_RETRIES",
    "ACTION_TIMEOUT_MS",
    "DEFAULT_SCROLL_PX",
    "DEFAULT_WAIT_MS",
    "JS_CLICK",
    "MAX_RETRIES",
    "NAVIGATE_TIMEOUT_MS",
    "Resolution",
    "classify_target",
    "click",
    "navigate",
    "resolve_locator",
    "scroll",
    "scroll_into_view",
    "type_text",
]

ACTION_RETRIES = 2
"""动作默认重试次数（初次尝试之外；总尝试次数 = 1 + retries）。"""

MAX_RETRIES = 5
"""重试次数上限：重试是"给页面一点时间"，再多就是白等（每次尝试最长 ACTION_TIMEOUT_MS）。"""

ACTION_TIMEOUT_MS = 5_000
"""单次动作尝试的上限（毫秒）：每次尝试重新 resolve + Playwright 自动等待可交互。"""

NAVIGATE_TIMEOUT_MS = 30_000
"""``goto`` 的上限（毫秒），与 Playwright 默认一致；显式写出以便日后配置化。"""

DEFAULT_WAIT_MS = 500
"""navigate 之后 ``networkidle`` 条件等待的上限（毫秒，D3）；0 = 跳过等待。"""

DEFAULT_SCROLL_PX = 500
"""scroll 未给 amount 时的滚动距离（像素）。"""

JS_CLICK = "el => el.click()"
"""click 的最后手段（P3-D2）：Playwright 判定"点不了"（被遮挡/不可见）时仍能触发。"""

_JS_SET_VALUE = """(el, value) => {
  el.focus();
  if (typeof el.value === 'string') {
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
    if (descriptor && descriptor.set) {
      descriptor.set.call(el, value);
    } else {
      el.value = value;
    }
  } else {
    el.textContent = value;
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}"""
"""type 的最后手段：赋值 + input/change 事件（**替换**语义，重放安全）。

``value`` 走原型上的 setter（等价于用户输入），否则 React 的 value tracker 会把程序化赋值
当成"没变化"而吞掉事件。
"""

_MAX_ERROR_CHARS = 300
"""单条错误描述写进结果 data 的上限（CLAUDE.md 教训 §4：进状态的数据一律截断）。"""

# ── 形态识别（ref → CSS → 文本） ──────────────────────────────────────────────

# HTML5 元素名：裸词只有在是已知 tag 时才按 CSS 解释（"input"/"video" → CSS，
# "Submit"/"搜索"/"Video" → 文本）。否则 page.locator("Submit") 永远匹配不到 <Submit>，
# 会把模型给的文本目标误判成 tag 选择器。全小写匹配：CSS tag 惯例小写，自然语言标签常首字母大写。
_HTML_TAGS = frozenset(
    "a abbr address area article aside audio b base bdi bdo blockquote body br button canvas "
    "caption cite code col colgroup data datalist dd del details dfn dialog div dl dt em embed "
    "fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 head header hgroup hr html i "
    "iframe img input ins kbd label legend li link main map mark menu meta meter nav noscript "
    "object ol optgroup option output p picture pre progress q rp rt ruby s samp script search "
    "section select slot small source span strong style sub summary sup table tbody td template "
    "textarea tfoot th thead time title tr track u ul var video wbr".split()
)

_REF_PREFIXES = ("aria-ref=", "ref=", "@")
_REF_BRACKET_RE = re.compile(r"^\[ref=(?P<label>[^\]]+)\]$")
_REF_BARE_RE = re.compile(r"^(?P<label>[eE]\d+|\d+)$")
_REF_HASH_RE = re.compile(r"^#(?P<label>\d+)$")
_REF_LABEL_RE = re.compile(r"^(?:[A-Za-z_][\w-]{0,31}|\d{1,32})$")
_REF_LOWER_RE = re.compile(r"^[eE]\d+$")

# CSS 的强结构字符：几乎不会出现在自然语言文本里（属性选择器 / id / 组合器）。
_CSS_STRONG_RE = re.compile(r"[\[\]#>~]")
_CSS_LEADING = "#.[*:"
_CSS_WEAK = "=:."
_COMBINATORS = frozenset({">", "+", "~"})


@dataclass(frozen=True)
class Resolution:
    """一次定位解析的结果：``kind`` ∈ ``{"ref","css","text"}``，``selector`` 交给 Playwright。"""

    kind: str
    selector: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.selector}"

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "selector": self.selector}


def _normalize_ref(label: str) -> str | None:
    """ref label 归一化：``E2`` → ``e2``；不是 label 形态返回 None。"""
    label = label.strip()
    if not _REF_LABEL_RE.match(label):
        return None
    return label.lower() if _REF_LOWER_RE.match(label) else label


def _ref_label(target: str) -> str | None:
    """识别 ref 形态的 target → Playwright 的 ref label。

    支持快照回显的各种写法：``e2`` / ``@e2`` / ``aria-ref=e2`` / ``ref=e2`` / ``[ref=e2]``，
    以及块式 YAML 的纯数字 label（``3`` / ``#3``——数字 id 不是合法 CSS，按 ref 处理）。
    ``#e2`` 是合法 CSS id，不在这里抢。
    """
    text = target.strip()
    if not text:
        return None
    lowered = text.lower()
    for prefix in _REF_PREFIXES:
        if lowered.startswith(prefix):
            return _normalize_ref(text[len(prefix) :])
    match = _REF_BRACKET_RE.match(text)
    if match:
        return _normalize_ref(match.group("label"))
    match = _REF_BARE_RE.match(text) or _REF_HASH_RE.match(text)
    return _normalize_ref(match.group("label")) if match else None


def _is_css_token(token: str) -> bool:
    """单个空格分隔 token 是否像 CSS（组合器 / 选择器起手 / 强结构字符 / 已知 tag）。"""
    if token in _COMBINATORS:
        return True
    if token[0] in _CSS_LEADING:
        return True
    if _CSS_STRONG_RE.search(token):
        return True
    if any(char in token for char in _CSS_WEAK):
        return True
    return token in _HTML_TAGS  # 全小写精确匹配（"video" 是 tag，"Video" 是文本）


def _looks_like_css(target: str) -> bool:
    """target 是否更像 CSS 选择器而不是文本（``div button`` 是 CSS，``Sign in`` 是文本）。"""
    text = target.strip()
    return bool(text) and all(_is_css_token(token) for token in text.split())


def classify_target(target: str) -> Resolution:
    """按序识别 target 的形态（ref → CSS → 文本），返回第一个命中的解释。

    纯字符串判定：不打探页面、不产生额外往返。空 target 抛 ``ValueError``
    （动作层会把它转成 ``invalid_target`` 结果）。
    """
    text = (target or "").strip()
    if not text:
        raise ValueError("target must be a non-empty string")
    label = _ref_label(text)
    if label is not None:
        return Resolution("ref", f"aria-ref={label}")
    if _looks_like_css(text):
        return Resolution("css", text)
    return Resolution("text", text)


def resolve_locator(page: Page, target: str, *, trace: list[Resolution] | None = None) -> Locator:
    """target → Playwright locator（多重回退：ref → CSS → 文本）。

    给出 ``trace`` 时把本次解析记进列表（调用方据此把解析路径写进结果 ``data``）。
    空 target 抛 ``ValueError``，由动作层转成 ``invalid_target`` 结果。
    """
    resolution = classify_target(target)
    if trace is not None:
        trace.append(resolution)
    locator = _build_locator(page, resolution)
    logger.debug("resolved target {!r} via {} → {}", target, resolution.kind, resolution.selector)
    return locator


def _build_locator(page: Page, resolution: Resolution) -> Locator:
    if resolution.kind == "text":
        return page.get_by_text(resolution.selector, exact=False)
    return page.locator(resolution.selector)


# ── 动作原语 ──────────────────────────────────────────────────────────────────


async def click(page: Page, target: str, *, retries: int = ACTION_RETRIES) -> dict[str, Any]:
    """点击 target：每次尝试重新 resolve；全部失败后用 JS 触发兜底。

    返回 ToolResult 兼容 dict（``status`` / ``content`` / ``data`` / ``error_type`` /
    ``error_message``），绝不抛异常。``data`` 含 ``resolution`` / ``attempts`` /
    ``js_fallback`` / ``errors`` / ``url``。
    """
    try:
        resolution = classify_target(target)
    except ValueError as exc:
        return _fail("click", "invalid_target", str(exc), {"target": target})

    errors: list[str] = []
    attempts = _attempt_count(retries)
    hit, primary_exc = await _run_attempts(
        page, target, lambda locator: locator.click(timeout=ACTION_TIMEOUT_MS), retries, errors
    )
    if hit is not None:
        return _ok(
            "click",
            f"已点击 {target}",
            _detail(resolution, attempts=hit, js_fallback=False, page=page, target=target),
        )

    try:
        await resolve_locator(page, target).evaluate(JS_CLICK, timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:  # JS 兜底也失败 → 结构化错误（绝不冒泡）
        errors.append(f"js fallback: {_describe(exc)}")
        return _fail(
            "click",
            _error_type(primary_exc or exc, fallback="action_failed"),
            f"点击失败：{target}（尝试 {attempts} 次 + JS 兜底）；最后错误：{errors[-1]}",
            _detail(
                resolution, attempts=attempts, js_fallback=True, page=page, target=target, errors=errors
            ),
        )
    return _ok(
        "click",
        f"已点击 {target}（JS 触发兜底）",
        _detail(resolution, attempts=attempts, js_fallback=True, page=page, target=target, errors=errors),
    )


async def type_text(
    page: Page, target: str, text: str, *, retries: int = ACTION_RETRIES
) -> dict[str, Any]:
    """把 ``text`` 填进 target：**先清空再填**（P3-D7 幂等，绝不追加）。

    每次尝试重新 resolve；全部失败后用 JS 赋值兜底（同样是替换语义，重放安全）。
    """
    try:
        resolution = classify_target(target)
    except ValueError as exc:
        return _fail("type", "invalid_target", str(exc), {"target": target, "text": text})

    async def _fill(locator: Locator) -> None:
        await locator.fill("", timeout=ACTION_TIMEOUT_MS)  # 先清空：重放同一动作不会追加
        await locator.fill(text, timeout=ACTION_TIMEOUT_MS)

    errors: list[str] = []
    attempts = _attempt_count(retries)
    hit, primary_exc = await _run_attempts(page, target, _fill, retries, errors)
    if hit is not None:
        return _ok(
            "type",
            f"已在 {target} 输入 {text!r}",
            _detail(
                resolution, attempts=hit, js_fallback=False, page=page, target=target, text=text
            ),
        )

    try:
        await resolve_locator(page, target).evaluate(
            _JS_SET_VALUE, text, timeout=ACTION_TIMEOUT_MS
        )
    except Exception as exc:
        errors.append(f"js fallback: {_describe(exc)}")
        return _fail(
            "type",
            _error_type(primary_exc or exc, fallback="action_failed"),
            f"输入失败：{target}（尝试 {attempts} 次 + JS 兜底）；最后错误：{errors[-1]}",
            _detail(
                resolution,
                attempts=attempts,
                js_fallback=True,
                page=page,
                target=target,
                text=text,
                errors=errors,
            ),
        )
    return _ok(
        "type",
        f"已在 {target} 输入 {text!r}（JS 赋值兜底）",
        _detail(
            resolution, attempts=attempts, js_fallback=True, page=page, target=target, text=text, errors=errors
        ),
    )


_SCROLL_SIGNS = {"down": (0, 1), "up": (0, -1), "right": (1, 0), "left": (-1, 0)}
_SCROLL_LABELS = {"down": "下", "up": "上", "right": "右", "left": "左"}


async def scroll(page: Page, direction: str = "down", amount: int | None = None) -> dict[str, Any]:
    """按方向滚动 ``amount`` 像素（默认 500）：鼠标滚轮事件，不依赖 locator。"""
    key = (direction or "").strip().lower()
    signs = _SCROLL_SIGNS.get(key)
    if signs is None:
        return _fail(
            "scroll",
            "invalid_direction",
            f"不支持的滚动方向：{direction!r}（可用：down / up / left / right）",
            {"direction": direction},
        )
    if amount is None:
        pixels = DEFAULT_SCROLL_PX
    else:
        try:
            pixels = abs(int(amount))
        except (TypeError, ValueError):
            return _fail(
                "scroll",
                "invalid_amount",
                f"amount 必须是像素整数：{amount!r}",
                {"direction": key, "amount": repr(amount)},
            )
    delta_x, delta_y = (sign * pixels for sign in signs)
    try:
        await page.mouse.wheel(delta_x, delta_y)
    except Exception as exc:
        return _fail(
            "scroll",
            _error_type(exc, fallback="action_failed"),
            f"滚动失败（{key} {pixels}px）：{_describe(exc)}",
            {"direction": key, "amount": pixels},
        )
    return _ok(
        "scroll",
        f"已向{_SCROLL_LABELS[key]}滚动 {pixels}px",
        {"direction": key, "amount": pixels, "delta": [delta_x, delta_y], "url": _page_url(page)},
    )


async def scroll_into_view(
    page: Page, target: str, *, retries: int = ACTION_RETRIES
) -> dict[str, Any]:
    """把 target 滚进可视区（click 前显式准备；Playwright 的 click 本身也会自动滚动）。"""
    try:
        resolution = classify_target(target)
    except ValueError as exc:
        return _fail("scroll_into_view", "invalid_target", str(exc), {"target": target})

    errors: list[str] = []
    attempts = _attempt_count(retries)
    hit, last_exc = await _run_attempts(
        page,
        target,
        lambda locator: locator.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS),
        retries,
        errors,
    )
    if hit is not None:
        return _ok(
            "scroll_into_view",
            f"已滚动到 {target}",
            _detail(resolution, attempts=hit, js_fallback=False, page=page, target=target),
        )
    return _fail(
        "scroll_into_view",
        _error_type(last_exc, fallback="action_failed"),
        f"滚动到目标失败：{target}（尝试 {attempts} 次）；最后错误：{errors[-1]}",
        _detail(
            resolution, attempts=attempts, js_fallback=False, page=page, target=target, errors=errors
        ),
    )


async def navigate(page: Page, url: str, *, wait_ms: int = DEFAULT_WAIT_MS) -> dict[str, Any]:
    """打开 url（D3）：``domcontentloaded`` 后有界等网络空闲；等待超时不致命。"""
    requested = (url or "").strip()
    if not requested:
        return _fail("navigate", "invalid_url", "url 不能为空", {"url": url})

    try:
        response = await page.goto(
            requested, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS
        )
    except Exception as exc:
        return _fail(
            "navigate",
            _error_type(exc, fallback="navigation_failed"),
            f"打开页面失败：{requested}（{_describe(exc)}）",
            {"url": requested, "requested_url": requested, "errors": [_describe(exc)]},
        )

    bound_ms = _wait_bound(wait_ms)
    waited, wait_timeout = False, False
    if bound_ms > 0:
        try:
            await page.wait_for_load_state("networkidle", timeout=bound_ms)
            waited = True
        except Exception as exc:  # 有界等待超时 = "页面还没静"，不是失败（D3）
            wait_timeout = True
            logger.debug("networkidle wait bounded out after {}ms: {}", bound_ms, _describe(exc))

    final_url = _page_url(page)
    content = f"已打开 {final_url or requested}"
    if wait_timeout:
        content += f"（网络未在 {bound_ms}ms 内空闲，已继续）"
    return _ok(
        "navigate",
        content,
        {
            "requested_url": requested,
            "url": final_url,
            "status": getattr(response, "status", None),
            "waited": waited,
            "wait_timeout": wait_timeout,
            "wait_ms": bound_ms,
        },
    )


# ── 内部 helper（全部 failsafe：不打印、不抛异常给调用方） ─────────────────────


def _attempt_count(retries: int) -> int:
    """总尝试次数 = 1 + retries；越界（负数 / 非数字 / 过大）一律收敛到 [0, MAX_RETRIES]。"""
    try:
        extra = int(retries)
    except (TypeError, ValueError):
        extra = ACTION_RETRIES
    return 1 + min(max(extra, 0), MAX_RETRIES)


def _wait_bound(wait_ms: int) -> int:
    """条件等待上限：非数字 → 默认；负数 → 0（跳过等待）。"""
    try:
        return max(0, int(wait_ms))
    except (TypeError, ValueError):
        return DEFAULT_WAIT_MS


async def _run_attempts(
    page: Page,
    target: str,
    action: Callable[[Locator], Awaitable[None]],
    retries: int,
    errors: list[str],
) -> tuple[int | None, BaseException | None]:
    """``resolve → action`` 最多 ``1 + retries`` 次；返回（成功的尝试序号 or None，最后一次异常）。

    每次尝试都重新 resolve（页面可能已变），每次失败都记进 ``errors``；
    调用方负责在全部失败后走 JS 兜底或交出错误结果。
    """
    total = _attempt_count(retries)
    last_exc: BaseException | None = None
    for attempt in range(1, total + 1):
        try:
            await action(resolve_locator(page, target))
        except Exception as exc:  # 任何动作失败都重试，绝不冒泡（CancelledError 不在 Exception 内）
            last_exc = exc
            errors.append(f"attempt {attempt}: {_describe(exc)}")
            continue
        return attempt, None
    return None, last_exc


def _detail(
    resolution: Resolution,
    *,
    attempts: int,
    js_fallback: bool,
    page: Page,
    target: str,
    errors: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """动作结果的 data 主体（页面上发生了什么，供 executor 与 trace 消费）。"""
    data: dict[str, Any] = {
        "target": target,
        "resolution": resolution.as_dict(),
        "attempts": attempts,
        "js_fallback": js_fallback,
        "url": _page_url(page),
    }
    if errors:
        data["errors"] = errors
    data.update(extra)
    return data


def _ok(action: str, content: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "success",
        "content": content,
        "data": {"action": action, **data},
        "error_type": "",
        "error_message": "",
    }


def _fail(
    action: str, error_type: str, error_message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "status": "error",
        "content": "",
        "data": {"action": action, **(data or {})},
        "error_type": error_type,
        "error_message": error_message,
    }


def _error_type(exc: BaseException | None, *, fallback: str) -> str:
    """把异常映射成稳定的 error_type（executor 的降级判断依赖它）。"""
    if isinstance(exc, PlaywrightTimeoutError):
        return "timeout"
    if isinstance(exc, PlaywrightError):
        return "playwright_error"
    return fallback


def _describe(exc: BaseException | None) -> str:
    """单行、截断的异常描述（Playwright 的 message 带多行 call log，进状态前必须压缩）。"""
    if exc is None:
        return ""
    message = " ".join(str(exc).split())
    if len(message) > _MAX_ERROR_CHARS:
        message = message[:_MAX_ERROR_CHARS] + "…"
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def _page_url(page: Page) -> str:
    """页面当前 URL；fake / 已关闭的页面不应让动作失败。"""
    try:
        return str(page.url)
    except Exception:
        return ""
