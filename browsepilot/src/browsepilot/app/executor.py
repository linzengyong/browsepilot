"""App-layer ToolExecutor: wires agent tool calls to the Tier1/2/3 services.

Tier3（T4 接线）要点：

- **6 个浏览器工具**（open/snapshot/click/type/scroll/execute_js）委托 ``app.browser.actions``，
  把 actions 的 ToolResult 兼容 dict 收成 :class:`ToolResult`；**失败即数据**（ToolExecutor
  契约），任何异常都在这一层变成结构化错误，绝不冒泡给图。
- **URL 安全（D4）**：``browser_open`` 必须在 ``actions.navigate`` 之前过
  ``NavigationPolicy.validate``——T2 已实证 ``navigate()`` 自身不设防（能开
  ``file:///etc/passwd``）。拒绝时 ``error_type="navigation_blocked"``，policy 的 reason
  （稳定契约 ``code: detail``）原样进 message/data，降级链与用户文案直接可用。
- **动作后复查 URL（I2）**：入参把关挡不住“动作把页面带出去”——点击链接/弹窗、``goto`` 跟随
  重定向、``execute_js`` 里的跳转都可能落在策略外（probe：``location.assign`` / ``location.replace`` /
  ``requestSubmit`` / ``textContent=`` / ``new Image().src=`` / ``window['fetch']``）。
  所以 browser_open 导航**成功**后、browser_click 与 browser_execute_js 返回前，一律用
  ``NavigationPolicy.validate(_page_url(page))`` 复查当前 URL；命中即 ``navigation_blocked``，
  **并隔离这一页**（丢引用 + 归还池）：后续调用不会继续在违规源上动作，必须先重新
  ``browser_open``（重新过策略门）。两条不复查的路径各有理由：读不出 URL（页面已关/驱动
  异常）不是“跳到了违规地址”；导航失败时读到的是浏览器错误页（``chrome-error://``），
  复查会把真实的 ``navigation_failed`` 伪装成策略拒绝。**残余**：重定向后导航失败（最终 URL
  未知）不在复查范围。
- **D5/D6 HITL 桥**：登录墙/人机验证/确认弹窗必须由人处理——``browser_open``（导航成功后）、
  ``browser_click``、``browser_type`` 之后采样页面（``page.content()`` 前 20KB + 可见密码框探测，
  都是 best-effort），交给 ``browser.detect.detect_intervention``；命中就把 ``needs_human=True`` /
  ``human_prompt`` / ``intervention`` 并进结果 ``data``，P2 的 HITL 机制自动接管
  （``graph.execute`` 见 ``data["needs_human"]`` 即转 hitl 节点 interrupt）——**图不需要改**。
  检测绝不改变动作本身的结果：``status``/``content``/``error_type`` 原样保留，检测失败
  （页面已关、content 读不出）当作“没发现”。成本是每个交互动作一次整页序列化（采样随后被
  截断到 20KB），这是 D5 的固有代价；也正因为有成本，观察类工具（snapshot/scroll/execute_js）
  不跑检测。
- **当前页跟踪**：executor 是 app 级共享对象，browser 工具共用**一个** current page
  （``browser_open`` 首次向池索取，之后复用；snapshot/click/type/scroll 都作用在它身上）。
  使用前查 ``is_closed()``（池到上限会回收最老的 busy page，``close()`` 会关掉全部 page）：
  死页立即丢引用，下次 ``browser_open`` 重新取页——否则 current page 指向死页后，每个浏览器
  调用都永久失败且无法恢复。``aclose()`` 显式把当前页归还池（executor 退役时调用；不调用则
  该池槽一直占着）——**尚未接到 app lifespan**（P4 议题，不在本次改动范围）。
  并发限制：多会话并行时看到的是同一个页面（会互相串页）；``self._page_lock`` 只保证
  "取页/换页"不交错（并发 open 不会各建一个 page），**不做会话隔离**——会话级隔离要把
  page 放进会话上下文（P4+ 议题）。demo 阶段接受该限制并在此显式记录；``aclose()``/隔离
  归还的页面同样在池里共享（多会话下别的消费者可能拿到，见 :meth:`aclose` 残余说明）。
- **池可选**：``browser=None``（Tier3 未接线）时 6 个工具一律返回
  ``error_type="browser_unavailable"``，图不需要特判 Tier3：策略链按既有降级链走到
  all_strategies_failed + 明确告知（诚实原则）。
- **execute_js 只读**（P3-D1「仅供提取」）：静态黑名单挡明显的写操作
  （fetch/localStorage/location 赋值与调用/click/submit/requestSubmit/DOM 赋值…），结果序列化后按
  ``MAX_JS_RESULT_CHARS`` 截断再进状态（CLAUDE.md 教训 §4）。黑名单是 best-effort，
  **不是沙箱**（动态拼写的成员访问如 ``window['loc'+'ation'].assign`` 仍可绕过）；补口是
  “动作后复查 URL”（见上）：真把页面带出去的动作会被拦在 ``navigation_blocked``。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from loguru import logger
from playwright.async_api import Page

from browsepilot.agent.tools import ToolResult, ToolSpec
from browsepilot.app.browser import actions, detect
from browsepilot.app.browser.browser_pool import BrowserPool, page_snapshot_refs
from browsepilot.app.browser.policy import NavigationPolicy
from browsepilot.app.config import Settings
from browsepilot.app.services.fetch import FetchError, PageFetchService
from browsepilot.app.services.search import SearchError, SearchService
from browsepilot.app.services.weather import CityNotFoundError, WeatherService

MAX_JS_RESULT_CHARS = 2000
"""``browser_execute_js`` 结果进状态前的上限（教训 §4：原始外部输出绝不整段塞进状态）。"""

_SNAPSHOT_NOTE = "（仅观察，未执行交互）"
"""browser_snapshot 的观察标注（M-b）：快照是“看见了什么”，不是“做了什么”。

答案节点只读 ``result_data``（agent/graph.py 的 answer），所以标注要进 ``data``；
``content`` 同步带上，保持两个面一致——免得答案模型把“看到了按钮”说成“点击了按钮”。
"""

_MAX_ERROR_CHARS = 300
"""单条错误描述写进结果的上限（与 actions/policy 同一约定）。"""

_BROWSER_MISSING_MSG = "浏览器不可用：AppToolExecutor 未注入 BrowserPool（Tier3 工具降级）"

_BROWSER_TOOLS = frozenset(
    {
        "browser_open",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_execute_js",
    }
)

_JS_WRITE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "网络请求（fetch/XMLHttpRequest/WebSocket/EventSource/sendBeacon）",
        re.compile(
            r"\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\b|\bEventSource\b|sendBeacon"
            r"|\bwindow\s*\[\s*['\"]fetch",  # window['fetch']('/api/...')（I2 探针）
            re.IGNORECASE,
        ),
    ),
    ("动态求值（eval / new Function）", re.compile(r"\beval\s*\(|\bnew\s+Function\s*\(", re.I)),
    (
        "DOM 写入（document.write / innerHTML / textContent / src 赋值）",
        re.compile(
            r"document\s*\.\s*write\b"
            r"|\.\s*innerHTML\s*="
            r"|insertAdjacentHTML"
            r"|\.\s*(textContent|innerText)\s*=(?!=)"  # document.body.textContent='x'（I2 探针）
            r"|\.\s*src\s*=(?!=)",  # new Image().src='...' 外传探针（I2 探针）
            re.I,
        ),
    ),
    (
        "持久化写入（cookie / storage / indexedDB）",
        re.compile(
            r"document\s*\.\s*cookie\s*="
            r"|(localStorage|sessionStorage)\s*\.\s*(setItem|removeItem|clear)"
            r"|\bindexedDB\b",
            re.I,
        ),
    ),
    (
        "导航（location 赋值/调用 / history 变更 / window.open）",
        re.compile(
            r"\blocation\s*(\.\s*(href|assign|replace|reload)\s*)?=(?!=)"
            # location.assign('...') / location.replace('/next')：I2 探针命中的调用形式。
            # 刻意不写 "\.\s*(assign|replace)\s*\("：那会连坐字符串的 str.replace(...)，
            # 而 innerText.replace(...) 正是只读提取的常用写法（见测试）。
            r"|\blocation\s*\.\s*(assign|replace)\s*\("
            r"|\blocation\s*\[\s*['\"](assign|replace)['\"]\s*\]\s*\("
            r"|\bhistory\s*\.\s*(pushState|replaceState|go|back|forward)"
            r"|\bwindow\s*\.\s*open\s*\(",
            re.I,
        ),
    ),
    (
        "表单/点击（submit / requestSubmit / click / dispatchEvent）",
        re.compile(
            r"\.\s*submit\s*\(|\brequestSubmit\s*\(|\.\s*click\s*\(|dispatchEvent", re.I
        ),
    ),
    ("剪贴板写入", re.compile(r"clipboard\s*\.\s*(write|writeText)\s*\(", re.I)),
)

_TOOL_SPECS = [
    ToolSpec(
        name="weather",
        description="查询指定中国城市的当前天气（温度/天气状况/湿度/风速）",
        parameters={"city": "string"},
    ),
    ToolSpec(name="search", description="搜索互联网获取信息", parameters={"query": "string"}),
    ToolSpec(
        name="fetch_page", description="抓取并提取指定 URL 的网页正文", parameters={"url": "string"}
    ),
    ToolSpec(
        name="browser_open",
        description="用浏览器打开指定 URL（导航前做 URL 安全校验），打开后成为当前页面",
        parameters={"url": "string"},
    ),
    ToolSpec(
        name="browser_snapshot",
        description="获取当前页面的无障碍树快照（可交互元素的 ref，如 e2；最多 30 条）",
        parameters={},
    ),
    ToolSpec(
        name="browser_click",
        description=(
            "点击当前页面上的元素（ref 如 e2，也可以是 CSS 选择器或可见文本；"
            "参数名 target 是 ref 的别名）"
        ),
        parameters={"ref": "string"},
    ),
    ToolSpec(
        name="browser_type",
        description=(
            "在当前页面元素里输入文本（先清空再填，重放安全；参数名 target 是 ref 的别名）"
        ),
        parameters={"ref": "string", "text": "string"},
    ),
    ToolSpec(
        name="browser_scroll",
        description="滚动当前页面（down/up/left/right，默认 500px）",
        parameters={"direction": "string"},
    ),
    ToolSpec(
        name="browser_execute_js",
        description="在当前页面执行只读 JS 表达式并返回结果（禁止写操作；结果截断后返回）",
        parameters={"expression": "string"},
    ),
]


class _PoolUnavailableError(RuntimeError):
    """池不可用（未 start / 建页失败）→ ``browser_unavailable``（内部信号，不冒泡出 executor）。"""


class AppToolExecutor:
    """Tier1/2/3 工具分发（agent 层只看到 :class:`ToolExecutor` 协议）。

    Tier3 是可选接线：``browser=None`` 时 6 个浏览器工具返回 ``browser_unavailable``；
    并发限制（共享一个 current page、非会话隔离）见模块 docstring。
    """

    def __init__(
        self,
        weather: WeatherService,
        search: SearchService,
        fetch: PageFetchService,
        *,
        browser: BrowserPool | None = None,
        policy: NavigationPolicy | None = None,
    ):
        self._weather = weather
        self._search = search
        self._fetch = fetch
        self._browser = browser
        if policy is None:
            # 配置来源单一：Settings（BP_ 环境 / .env）。测试可注入构造好的策略。
            settings = Settings()
            policy = NavigationPolicy(settings.navigation_policy, settings.allowed_domains)
        self._policy = policy
        #: 当前页（browser 工具共享；首次 browser_open 向池索取，之后复用）
        self._page: Page | None = None
        #: 串行化"取当前页 / 换当前页"（并发限制见模块 docstring）
        self._page_lock = asyncio.Lock()

    async def list_tools(self) -> list[ToolSpec]:
        return _TOOL_SPECS

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            if name == "weather":
                w = await self._weather.get(arguments["city"])
                return ToolResult(
                    status="success",
                    content=f"{w.city}：{w.conditions}，{w.temperature_c}°C",
                    data=w.model_dump(),
                )
            if name == "search":
                results = await self._search.search(arguments["query"])
                return ToolResult(
                    status="success",
                    content="\n".join(f"- {r.title} {r.url}" for r in results[:5]),
                    data={"results": [r.model_dump() for r in results]},
                )
            if name == "fetch_page":
                page = await self._fetch.fetch(arguments["url"])
                return ToolResult(status="success", content=page.text, data=page.model_dump())
            if name in _BROWSER_TOOLS:
                return await self._call_browser(name, arguments)
            return ToolResult(
                status="error", error_type="unknown_tool", error_message=f"unknown tool: {name}"
            )
        except CityNotFoundError as exc:
            return ToolResult(status="error", error_type="city_not_found", error_message=str(exc))
        except (SearchError, FetchError) as exc:
            return ToolResult(status="error", error_type="service_error", error_message=str(exc))

    # ── Tier3：浏览器工具（可选接线；失败即数据） ──────────────────────────────

    async def _call_browser(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """浏览器工具入口：池缺失 → browser_unavailable；其余异常 → 结构化错误。"""
        if self._browser is None:
            return _browser_error("browser_unavailable", _BROWSER_MISSING_MSG)
        try:
            async with self._page_lock:
                return await self._dispatch_browser(name, arguments)
        except _PoolUnavailableError as exc:
            return _browser_error("browser_unavailable", f"浏览器池不可用：{exc}")
        except Exception as exc:  # 兜底：ToolExecutor 契约是"失败即数据"，绝不冒泡
            logger.warning("browser tool {} failed unexpectedly: {}", name, exc)
            return _browser_error("browser_error", f"浏览器工具 {name} 异常：{_describe(exc)}")

    async def _dispatch_browser(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "browser_open":
            return await self._browser_open(arguments)
        page = self._current_page()  # 池回收过的死页不接（I3），先丢引用
        if page is None:
            return _browser_error(
                "no_open_page", "还没有打开的页面：请先调用 browser_open(url) 再观察或操作"
            )
        if name == "browser_snapshot":
            return await self._browser_snapshot(page)
        if name == "browser_click":
            result = _to_tool_result(await actions.click(page, _arg(arguments, "ref", "target")))
            result = await self._with_intervention(page, result)  # D5/D6：登录墙/验证码 → HITL
            # 点击可能跟着链接/弹窗跳走（I2）：无论成败都复查当前 URL
            return await self._revalidate_after_action(page, "browser_click", result)
        if name == "browser_type":
            # text 故意的 Any：非 str 由 actions 结构化拒绝（invalid_text），不在这里强转
            text: Any = arguments.get("text")
            result = _to_tool_result(
                await actions.type_text(page, _arg(arguments, "ref", "target"), text)
            )
            return await self._with_intervention(page, result)  # D5/D6
        if name == "browser_scroll":
            direction = _arg(arguments, "direction")
            return _to_tool_result(
                await actions.scroll(
                    page, "down" if direction is None else direction, arguments.get("amount")
                )
            )
        if name == "browser_execute_js":
            return await self._browser_execute_js(page, arguments)
        return _browser_error("unknown_tool", f"unknown browser tool: {name}")

    async def _browser_open(self, arguments: dict[str, Any]) -> ToolResult:
        """策略门 → navigate（D4：把关必须在动作之前，拒绝路径不建页不导航）→ 检测登录/验证码
        （D5/D6）→ 复查落点（I2）。

        检测放在复查之前：动作就是动作，先如实记下“页面上要人工”再判落点合不合规；落点违规时
        复查会整体覆盖结果（安全门优先，见 :meth:`_revalidate_after_action`）。导航失败路径
        直接返回：页面停在浏览器错误页（``chrome-error://``），采样没有意义。
        """
        url = _arg(arguments, "url")
        allowed, reason = self._policy.validate(url)
        if not allowed:
            return _browser_error(
                "navigation_blocked",
                f"URL 被安全策略拒绝：{reason}",
                data={"action": "browser_open", "url": _preview(url), "reason": reason},
            )
        page = self._current_page()
        if page is None:
            page = await self._acquire_page()
        result = _to_tool_result(await actions.navigate(page, url))
        if result.status != "success":
            # 失败即数据：导航失败时页面停在浏览器错误页（chrome-error://…），复查会把
            # 真实的 navigation_failed 伪装成策略拒绝，所以失败路径原样返回
            return result
        result = await self._with_intervention(page, result)  # D5/D6：登录墙/验证码 → HITL
        return await self._revalidate_after_action(page, "browser_open", result)

    async def _browser_snapshot(self, page: Page) -> ToolResult:
        try:
            refs = await page_snapshot_refs(page)
        except Exception as exc:  # 页面已关闭 / aria_snapshot 不支持
            return _browser_error(
                "snapshot_failed",
                f"页面快照失败：{_describe(exc)}",
                data={"action": "snapshot", "url": _page_url(page)},
            )
        url = _page_url(page)
        if not refs.strip():
            # 空快照按失败处理：让策略链继续降级，而不是把"什么都没有"当成功汇报
            return _browser_error(
                "empty_snapshot",
                "页面没有可交互元素（快照为空）",
                data={"action": "snapshot", "url": url, "refs": ""},
            )
        return ToolResult(
            status="success",
            content=f"{refs}\n{_SNAPSHOT_NOTE}",
            data={"action": "snapshot", "url": url, "refs": refs, "note": _SNAPSHOT_NOTE},
        )

    async def _browser_execute_js(self, page: Page, arguments: dict[str, Any]) -> ToolResult:
        expression = _arg(arguments, "expression", "script", "code")
        if not isinstance(expression, str) or not expression.strip():
            return _browser_error(
                "invalid_expression",
                "expression 必须是非空字符串（只读 JS 表达式，如 document.title）",
                data={"action": "execute_js", "expression": _preview(expression)},
            )
        violation = _js_write_violation(expression)
        if violation:
            return _browser_error(
                "read_only_violation",
                f"browser_execute_js 只允许只读表达式（命中写入模式：{violation}）",
                data={
                    "action": "execute_js",
                    "expression": _preview(expression),
                    "pattern": violation,
                },
            )
        try:
            raw = await page.evaluate(expression)
        except Exception as exc:
            # 表达式已经跑过了（可能已经跳转）：失败路径也复查 URL（I2）
            return await self._revalidate_after_action(
                page,
                "browser_execute_js",
                _browser_error(
                    "evaluate_failed",
                    f"JS 表达式执行失败：{_describe(exc)}",
                    data={
                        "action": "execute_js",
                        "url": _page_url(page),
                        "expression": _preview(expression),
                    },
                ),
            )
        text = _stringify(raw)
        truncated = len(text) > MAX_JS_RESULT_CHARS
        return await self._revalidate_after_action(
            page,
            "browser_execute_js",
            ToolResult(
                status="success",
                content=text[:MAX_JS_RESULT_CHARS],
                data={
                    "action": "execute_js",
                    "url": _page_url(page),
                    "expression": _preview(expression),
                    "result": text[:MAX_JS_RESULT_CHARS],
                    "truncated": truncated,
                },
            ),
        )

    # ── D5/D6：登录/验证码检测（动作后 best-effort，命中即上 HITL 桥） ──────────────

    async def _with_intervention(self, page: Page, result: ToolResult) -> ToolResult:
        """动作后检测登录/验证码/弹窗（D5/D6）：命中就把 HITL 载荷并进 ``data``。

        只增三个键（``needs_human`` / ``human_prompt`` / ``intervention``），
        ``status`` / ``content`` / ``error_type`` 原样保留——动作本身的结果才是数据；
        其中 ``needs_human`` 在**成功**结果上是“转 hitl 节点暂停”的开关（graph.execute），
        在错误结果上只是诊断信息（错误路径继续走降级链，绝不把失败伪装成“需要人工”的成功）。
        检测失败（页面已关 / content 读不出）时原样返回：检测是 best-effort，绝不破坏动作。
        """
        intervention = await _page_intervention(page)
        if intervention is None:
            return result
        return result.model_copy(
            update={
                "data": {
                    **result.data,
                    "needs_human": True,
                    "human_prompt": intervention.human_prompt,
                    "intervention": intervention.kind,
                }
            }
        )

    # ── 当前页生命周期（I2 隔离 / I3 恢复与释放） ─────────────────────────

    def _current_page(self) -> Page | None:
        """当前页（使用前调用）：被池回收过（``is_closed()``）就丢引用，等重新取页。

        池到上限会回收最老的 busy page（``new_page()``），``close()`` 会关全部 page：
        不检查的话 ``self._page`` 永远指向死页，之后每个浏览器调用都失败且无法恢复
        （那个池槽也永远不释放）。
        """
        page = self._page
        if page is None:
            return None
        if _page_closed(page):
            logger.info("current page is closed (evicted/closed by pool); dropping reference")
            self._page = None
            return None
        return page

    async def _revalidate_after_action(
        self, page: Page, action: str, result: ToolResult
    ) -> ToolResult:
        """动作后复查当前 URL（I2）：入参把关挡不住“动作把页面带出去”（链接/弹窗/重定向/JS 跳转）。

        放行 → 原结果原样返回（动作本身的结果才是数据）；拦截 → ``navigation_blocked``
        + **隔离当前页**（丢引用 + 归还池）：后续调用不会继续在违规源上动作，必须先
        ``browser_open`` 重新过策略门。URL 读不出来（页面已关/驱动异常）时不拦——
        那不是“跳到了违规地址”，交给 closed-page 恢复路径（:meth:`_current_page`）。
        """
        url = _page_url(page)
        if not url:
            return result
        allowed, reason = self._policy.validate(url)
        if allowed:
            return result
        await self._quarantine_page(page)
        return _browser_error(
            "navigation_blocked",
            f"动作后页面跳转到策略不允许的 URL：{reason}",
            data={
                "action": action,
                "url": _preview(url),
                "reason": reason,
                "page_released": True,
            },
        )

    async def _quarantine_page(self, page: Page) -> None:
        """把“跳到策略外”的页面从当前页摘掉并归还池（best-effort，绝不覆盖动作结果）。"""
        self._page = None
        await self._release_page(page)

    async def _release_page(self, page: Page | None) -> None:
        """把页面还给池（best-effort）：池不可用/未实现 release/页面已关时静默跳过。

        已关闭的页面**不**归还：``new_page()`` 会把 idle 里的页原样取走，往 idle 放死页
        等于把故障传染给下一个调用者。
        """
        if page is None or self._browser is None or _page_closed(page):
            return
        try:
            await self._browser.release(page)
        except Exception as exc:  # 池已关 / 替身没实现 release：不因此改变动作结果
            logger.debug("page release failed: {}", _describe(exc))

    async def aclose(self) -> None:
        """释放当前页占用的池槽（置空引用 + 归还池；幂等）：不调则该池槽一直占着。

        残余限制（与模块 docstring 的共享页限制同源）：归还的页面仍停在最后打开的 URL 上，
        多会话并发时别的消费者可能拿到它；本 executor 随后会重新取页（``_current_page``），
        不会继续在旧页上动作。尚未接到 app lifespan（P4 议题）。
        """
        page, self._page = self._page, None
        await self._release_page(page)

    async def _acquire_page(self) -> Page:
        """取当前页（首次向池索取并记住）；池不可用 → :class:`_PoolUnavailableError`。"""
        pool = self._browser
        if pool is None:  # _call_browser 已拦住；这里只是给类型收窄
            raise _PoolUnavailableError(_BROWSER_MISSING_MSG)
        try:
            self._page = await pool.new_page()
        except Exception as exc:  # 未 start() / 启动失败 / 浏览器崩了
            raise _PoolUnavailableError(f"BrowserPool.new_page() 失败（{_describe(exc)}）") from exc
        return self._page


# ── 内部 helper（全部 failsafe：不抛异常给调用方） ────────────────────────────


def _arg(arguments: dict[str, Any], *names: str) -> Any:
    """取第一个存在的参数值（宽容别名：模型可能写 ``target`` 而非 ``ref``）。"""
    for name in names:
        if name in arguments and arguments[name] is not None:
            return arguments[name]
    return None


def _browser_error(
    error_type: str, message: str, *, data: dict[str, Any] | None = None
) -> ToolResult:
    """Tier3 结构化降级（对齐 actions 的失败形状：status=error + 空 content + 有界 message）。"""
    return ToolResult(status="error", error_type=error_type, error_message=message, data=data or {})


def _to_tool_result(result: dict[str, Any]) -> ToolResult:
    """actions 的 ToolResult 兼容 dict → ToolResult（显式取字段，多余键不影响边界）。

    ``status`` 只认 ``"success"``，其余一律 ``"error"``（fail-closed：绝不因字段异常假成功）。
    """
    return ToolResult(
        status="success" if result.get("status") == "success" else "error",
        content=result.get("content", ""),
        data=result.get("data") or {},
        error_type=result.get("error_type", ""),
        error_message=result.get("error_message", ""),
    )


def _js_write_violation(expression: str) -> str | None:
    """返回命中的写入模式描述（None = 没有明显写操作）。best-effort 静态检查，不是沙箱。"""
    for label, pattern in _JS_WRITE_PATTERNS:
        if pattern.search(expression):
            return label
    return None


def _stringify(value: Any) -> str:
    """JS 结果 → 字符串（dict/list 走 JSON，保持结构与可读性）。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _describe(exc: BaseException) -> str:
    """单行、截断的异常描述（进状态的数据一律有界：教训 §4）。"""
    message = " ".join(str(exc).split())
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return text if len(text) <= _MAX_ERROR_CHARS else text[:_MAX_ERROR_CHARS] + "…"


def _preview(value: Any, *, limit: int = _MAX_ERROR_CHARS) -> str:
    """非字符串入参的短 repr（截断后再进 data）。"""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _page_url(page: Page) -> str:
    """页面当前 URL；fake / 已关闭的页面不应让工具失败。"""
    try:
        return str(page.url)
    except Exception:
        return ""


def _page_closed(page: Page) -> bool:
    """页面是否已被池回收/关闭；读不出状态（替身、驱动异常）时按“可用”处理。"""
    try:
        return bool(page.is_closed())
    except Exception:
        return False


# ── D5/D6：页面采样（best-effort，绝不抛） ────────────────────────────────────


async def _sample_html(page: Page) -> str:
    """页面 HTML 采样：前 ``detect.MAX_HTML_SAMPLE_CHARS`` 字符、小写；读不出当空串。

    ``Page.content()`` 是整页序列化（协议没有“只取前 20KB”），截断发生在取回之后：
    这是 D5 的固有代价（每个交互动作一次），换来的是确定性字符串判定与零额外往返。
    """
    try:
        html = await page.content()
    except Exception as exc:  # 页面已关 / 驱动异常：检测降级，不影响动作结果
        logger.debug("detector: page.content() failed: {}", _describe(exc))
        return ""
    if not isinstance(html, str):
        return ""
    limit = detect.MAX_HTML_SAMPLE_CHARS
    return html[:limit].lower()


async def _has_password_field(page: Page) -> bool:
    """页面上是否有**可见**的密码输入框（探测失败当 False）。

    只认 ``:visible``：真实页面里 DOM 中藏着登录弹窗（``display:none`` 的登录表单）很常见，
    把“DOM 里存在”当成“页面上要登录”会直接误报，而误报要花掉唯一的人工暂停机会。
    """
    try:
        return await page.locator("input[type=password]:visible").count() > 0
    except Exception as exc:
        logger.debug("detector: password probe failed: {}", _describe(exc))
        return False


async def _page_intervention(page: Page) -> detect.Intervention | None:
    """动作后的页面 → 登录/验证码/弹窗判定（D6）；best-effort，异常一律当“未发现”。"""
    try:
        return detect.detect_intervention(
            _page_url(page), await _sample_html(page), await _has_password_field(page)
        )
    except Exception as exc:  # 纯函数不该抛；这是“检测绝不破坏动作”的最后一道防线
        logger.debug("detector: detect_intervention failed: {}", _describe(exc))
        return None
