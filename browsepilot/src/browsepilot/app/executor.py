"""App-layer ToolExecutor: wires agent tool calls to the Tier1/2/3 services.

Tier3（T4 接线）要点：

- **6 个浏览器工具**（open/snapshot/click/type/scroll/execute_js）委托 ``app.browser.actions``，
  把 actions 的 ToolResult 兼容 dict 收成 :class:`ToolResult`；**失败即数据**（ToolExecutor
  契约），任何异常都在这一层变成结构化错误，绝不冒泡给图。
- **URL 安全（D4）**：``browser_open`` 必须在 ``actions.navigate`` 之前过
  ``NavigationPolicy.validate``——T2 已实证 ``navigate()`` 自身不设防（能开
  ``file:///etc/passwd``）。拒绝时 ``error_type="navigation_blocked"``，policy 的 reason
  （稳定契约 ``code: detail``）原样进 message/data，降级链与用户文案直接可用。
- **动作后复查 URL（I2）+ 入口复查（I1）**：入参把关挡不住“动作把页面带出去”——点击链接/弹窗、
  ``goto`` 跟随重定向、``execute_js`` 里的跳转都可能落在策略外（probe：``location.assign`` /
  ``location.replace`` / ``requestSubmit`` / ``textContent=`` / ``new Image().src=`` / ``window['fetch']``）。
  **复查点（全部四处）**：① ``browser_open`` 导航成功后（拦重定向落点）；② ``browser_click`` 返回前；
  ③ ``browser_type`` 返回前（I1：JS 写值兜底同样能带走页）；④ ``browser_execute_js`` 返回前（成功与失败两条路径）。
  **入口复查（I1）**：``browser_snapshot`` 使用当前页之前也看一遍它在哪——只在动作后复查的话，
  “页面已经被带出去、接下来只读不写”的路径就漏了（一直在违规页上取快照）。
  命中即 ``navigation_blocked``，**并弃用这一页**（I2：丢引用 + ``discard()``，**不** release）：
  后续调用不会继续在违规源上动作，必须先重新 ``browser_open``（重新过策略门）；
  归还池等于把违规源（连同它的登录态/内容）交给下一个消费者。
  三类“不拦”各有理由：读不出 URL（页面已关/驱动异常）不是“跳到了违规地址”；
  导航失败时读到的是浏览器错误页（``chrome-error://chromewebdata/``）——它的 scheme 不在白名单，
  直接 validate 会把真实的 ``navigation_failed``（DNS 失败/连接被拒，P4 高频）伪装成策略拒绝，
  所以按**导航失败**如实上报 + ``discard``（C3b）；``about:blank`` 是新建页面的初始状态，
  既不是跳转也不是违规。**入口守卫与动作后复查共用同一个有序判定**（F1：错误页 → 空白页 → 策略，
  顺序不得倒过来 —— ``about:neterror`` 曾因空白页先命中而在入口路径上被静默放行，
  ``chrome-error://`` 则被报成“安全策略拒绝”）。**残余**：重定向后导航失败（最终 URL 未知）
  不在复查范围。
- **跨页点击的假失败（C3a）**：点击触发导航时 Playwright 的 ``click`` 等待会被导航打断并报
  timeout，而动作其实已经生效（T6 冒烟实测 1/2）——失败前记下 ``url_before``，失败且
  ``error_type == timeout``、去掉 ``#fragment`` 后 URL 真的变了（且新地址不是错误页/空白页）
  → 按“已生效”返回成功 + ``data["navigated"]=True``，原错误保留在
  ``data["error_before_navigation"]``。URL 没变 / 只有页内锚点变化 / 非 timeout 档都是真失败，
  **不提升**（F2：把没生效的点击记成成功同样是错的，且假成功更难查）。
- **D5/D6 HITL 桥**：登录墙/人机验证/确认弹窗必须由人处理——``browser_open``（导航成功后）、
  ``browser_click``、``browser_type`` 之后采样页面（**渲染后可见文本**：``body.inner_text()`` 的前
  20KB + 可见密码框探测，都是 best-effort），交给 ``browser.detect.detect_intervention``；
  ``high`` 置信命中就把 ``needs_human=True`` / ``human_prompt`` / ``intervention`` 并进结果 ``data``
  （``medium`` 只记 ``data["intervention"]`` + 日志、**不暂停**——F2：每次运行只有一次人工暂停
  机会，弱文本信号不配花掉它），P2 的 HITL 机制自动接管（``graph.execute`` 见
  ``data["needs_human"]`` 即转 hitl 节点 interrupt）——**图不需要改**。检测绝不改变动作本身的结果：
  ``status``/``content``/``error_type`` 原样保留，检测失败（页面已关、可见文本读不出）当作“没发现”。
  采样源必须是**可见文本**而不能是原始 HTML（F1）：源码里的 ``display:none`` 隐藏登录下拉、
  ``<script>`` 与 i18n bundle 会让关键词规则误报（实例：电商产品页的可见「登录」导航链接 +
  隐藏登录下拉 → 误报 HITL），而误报要花掉唯一的人工暂停机会。成本是每个交互动作一次
  innerText 提取（随后被截断到 20KB）；也正因为有成本，观察类工具（snapshot/scroll/execute_js）
  不跑检测。
- **当前页跟踪（P5：按 asyncio 上下文隔离）**：executor 仍是 app 级共享对象，但"当前页"放进
  ``ContextVar``（``_page_var``）——**每个 asyncio 上下文/任务各一张页**，别的上下文看不到、
  也不会把这一页导航走（旧行为是 executor 级共享一页：多客户端并发时 B 的 ``browser_open``
  会把 A 的页带走，A 的 snapshot 看到的是 B 的页面——P3 final review #5 要求在 A2A 落地前解决）。同一上下文内（``browser_open`` →
  ``browser_snapshot`` → click…）依旧复用同一张页，P4 的语义不变。``_page_lock`` 随之**收窄到
  池操作**（取页的"检查-动作"对，:meth:`_acquire_page`）：页面动作不再全局串行 —— 各上下文
  各跑各的页，这是 A2A 多客户端并行的前提。
  使用前查 ``is_closed()``（池到上限会回收最老的 busy page，``close()`` 会关掉全部 page）：
  死页立即丢引用，下次 ``browser_open`` 重新取页——否则当前页指向死页后，每个浏览器
  调用都永久失败且无法恢复。**失败导航的页面同样立即弃用**（``_discard_page``，F4）：它停在
  ``chrome-error://`` 且错误页导航可能还在飞，复用同一页的下一次 ``goto`` 会被它打断
  （T6 冒烟 S6 确定性复现的假失败）。``aclose()`` 把本 executor **借出的全部页面**归还池
  （``_held_pages``：跨上下文，因为逐场景 teardown 跑在外层任务里，见 :meth:`aclose`）；
  **尚未接到 app lifespan**（P4 议题，不在本次改动范围）。
  残余（逐条记录）：① 页不再跨上下文复用——一个上下文首次 ``browser_open`` 取一张新页，上下文
  结束后无人归还（除非 ``aclose()`` 被调 / 池到上限回收最老的 busy 页），app 侧没接 lifespan
  归还点 → 长跑会有页 churn；② 同一上下文内的**同页并发**动作不再有全局互斥（图里的工具
  调用本就是顺序 ``await``；将来若在会话内并发编排工具，得自己串行）；③ 归还（``aclose``）与
  弃用后的页仍停在最后打开的 URL 上，多会话下别的消费者可能从 idle 取到它。
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
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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

_SAMPLE_TIMEOUT_MS = 5_000
"""可见文本采样的有界等待（毫秒）：与 ``actions.ACTION_TIMEOUT_MS`` 同量级。

``body`` 在导航完成后一般早已就位，但 locator 的自动等待默认 30s——采样是附加的
best-effort 动作，绝不能把工具调用拖住。
"""

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
            # 兜底：按 Settings（BP_ 环境 / .env）自建 —— 只有**独立构造**（测试/脚本）才走到这里。
            # 组合根（app.main._default_app / eval.run_eval._real_stack）必须显式传入：
            # 不传的话这里会把调用方交付的 settings 重新读一遍环境，静默覆盖它（I3）。
            settings = Settings()
            policy = NavigationPolicy(settings.navigation_policy, settings.allowed_domains)
        self._policy = policy
        #: 当前页（同一个上下文里的 6 个 browser 工具共用；首次需要页时向池索取，之后复用）：
        #: **上下文局部**变量（P5）——每个 asyncio 任务/图节点各一张页，别的会话看不到也拿不走。
        self._page_var: ContextVar[Page | None] = ContextVar(
            f"browsepilot_current_page_{id(self)}", default=None
        )
        #: 本 executor 从池里借出、尚未归还/弃用的全部页面（跨上下文——上下文结束后没人替它
        #: 归还，而 ``aclose()`` 由**外层任务**调用；见 :meth:`aclose`）。
        self._held_pages: list[Page] = []
        #: 池操作保护：取页的"检查-动作"对（:meth:`_acquire_page`）。不再用来串行不同上下文的
        #: 页面动作（页面已按上下文隔离，见模块 docstring 的"当前页跟踪"）。
        self._page_lock = asyncio.Lock()

    async def list_tools(self) -> list[ToolSpec]:
        return _TOOL_SPECS

    def attach_browser(self, browser: BrowserPool | None) -> None:
        """绑定/解绑浏览器池（组合根在池 ``start()`` 之后调用；``None`` = Tier3 降级）。

        公开 API，取代 lifespan 直接写 ``executor._browser``（T4-wire 的私有属性访问）：
        不变式与 :func:`browsepilot.app.main._bind_executor_browser` 一致 —— 启动完成后
        持有的要么是**已启动**的池，要么是 None（6 个浏览器工具统一 ``browser_unavailable``）。
        构造参数 ``browser`` 仍然可用（测试 / 同步装配）；只有池的生命周期归 lifespan 时
        才需要这个接口（``start()`` 是异步的）。
        """
        self._browser = browser

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
        """浏览器工具入口：池缺失 → browser_unavailable；其余异常 → 结构化错误。

        P5 起这里**不再**全局串行（旧行为：``async with self._page_lock`` 包住整次派发）：页面已经
        按上下文隔离，不同会话的动作互不相干，各跑各的；``_page_lock`` 收窄到取页。同一张页上的
        动作仍应由调用方顺序发起（图里的工具调用本就是顺序 ``await``）。
        """
        if self._browser is None:
            return _browser_error("browser_unavailable", _BROWSER_MISSING_MSG)
        try:
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
            guard = await self._guard_current_url(page, "browser_snapshot")  # I1：入口复查
            if guard is not None:
                return guard
            return await self._browser_snapshot(page)
        if name == "browser_click":
            # C3a：点击可能把页面带走，失败时靠动作前的 URL 判断它有没有真的生效
            url_before = _page_url(page)
            result = _to_tool_result(await actions.click(page, _arg(arguments, "ref", "target")))
            result = await self._with_intervention(page, result)  # D5/D6：登录墙/验证码 → HITL
            result = _promote_click_that_navigated(result, url_before, page)  # C3a
            # 点击可能跟着链接/弹窗跳走（I2）：无论成败都复查当前 URL
            return await self._revalidate_after_action(page, "browser_click", result)
        if name == "browser_type":
            # text 故意的 Any：非 str 由 actions 结构化拒绝（invalid_text），不在这里强转
            text: Any = arguments.get("text")
            result = _to_tool_result(
                await actions.type_text(page, _arg(arguments, "ref", "target"), text)
            )
            result = await self._with_intervention(page, result)  # D5/D6
            # I1：type 的 JS 写值兜底同样能把页面带出去 —— 返回前也复查落点
            return await self._revalidate_after_action(page, "browser_type", result)
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
        直接返回：页面停在浏览器错误页（``chrome-error://``），采样没有意义，而且**该页必须弃用**
        （F4，见 :meth:`_discard_page`）。
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
        result = _navigation_failure(_to_tool_result(await actions.navigate(page, url)))
        if result.status != "success":
            # 失败即数据：导航失败时页面停在浏览器错误页（chrome-error://…），复查会把
            # 真实的 navigation_failed 伪装成策略拒绝，所以失败路径原样返回（只做归因归一化，
            # 见 :func:`_navigation_failure`）。
            # 但这一页不能再复用（F4）：错误页自身的导航可能还在飞，下一次 goto 会被它打断
            # （T6 冒烟 S6 确定性复现的假失败）——弃用 + 下次自动重取干净页。
            await self._discard_page(page)
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

    # ── D5/D6：登录/验证码检测（动作后 best-effort；high 命中上 HITL 桥，medium 只记录） ──

    async def _with_intervention(self, page: Page, result: ToolResult) -> ToolResult:
        """动作后检测登录/验证码/弹窗（D5/D6）：按命中**置信度**决定要不要上 HITL 桥（F2）。

        - ``high``（登录 URL / 可见密码框 / 验证码短语）：三个键一起并进 ``data``
          （``needs_human`` / ``human_prompt`` / ``intervention``）。
        - ``medium``（中文关键词组 / 验证码品牌词 / 确认弹窗短语）：只记
          ``data["intervention"]`` + 日志，**不置 needs_human**——每次运行只有一次人工暂停机会
          （``graph._route_execute_result`` 在 ``hitl_triggered_count > 0`` 后不再中断），
          弱信号（帮助页文案、页脚品牌词、无可见挑战的隐形 reCAPTCHA）不配把它花掉。

        ``status`` / ``content`` / ``error_type`` 一律原样保留——动作本身的结果才是数据；
        其中 ``needs_human`` 在**成功**结果上是“转 hitl 节点暂停”的开关（graph.execute），
        在错误结果上只是诊断信息（错误路径继续走降级链，绝不把失败伪装成“需要人工”的成功）。
        检测失败（页面已关 / 可见文本读不出）时原样返回：检测是 best-effort，绝不破坏动作。
        """
        intervention = await _page_intervention(page)
        if intervention is None:
            return result
        if intervention.confidence != "high":
            logger.info(
                "detector: {} intervention ({}); recording only, not pausing HITL",
                intervention.kind,
                intervention.reason,
            )
            return result.model_copy(
                update={"data": {**result.data, "intervention": intervention.kind}}
            )
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
        """当前页（使用前调用）：本上下文没有页就返回 None；被池回收过（``is_closed()``）就丢引用。

        页面是**上下文局部**的（P5，见模块 docstring）：别的会话/任务看不到这一页，也就不会串页。
        死页处理与 P4 相同：不检查的话 ``_page_var`` 永远指向死页，之后每个浏览器调用都失败且
        无法恢复（那个池槽也永远不释放）。
        """
        page = self._page_var.get()
        if page is None:
            return None
        if _page_closed(page):
            logger.info("current page is closed (evicted/closed by pool); dropping reference")
            self._forget_page(page)
            return None
        return page

    def _forget_page(self, page: Page) -> None:
        """丢掉对某页的引用：清本上下文的当前页（若正持有）+ 从借出记录里剔掉。

        死页、弃用页、归还页三条路径共用 —— 引用不丢的话，归还过的页会被再归还一次、或者死页
        被当成还能用的页（旧实现里只有一个 ``self._page``，这里是它的多上下文版本）。
        """
        if self._page_var.get() is page:
            self._page_var.set(None)
        self._held_pages = [held for held in self._held_pages if held is not page]

    async def _guard_current_url(self, page: Page, action: str) -> ToolResult | None:
        """**入口复查**（I1）：使用当前页之前先看它在哪；返回 None = 放行。

        为什么观察类工具也要看：复查只在**动作后**做，覆盖不到“页面已经带出去了但下一个调用
        只读不写”的路径——一直在违规页上取快照等于假装没看见（本入口目前只装在
        ``browser_snapshot`` 上；交互工具的落点由它们自己的动作后复查接住）。

        判定与动作后复查**共用** :meth:`_classify_page_url`（F1：顺序一致，错误页先于策略）——
        停在 ``chrome-error://chromewebdata/`` 的页面（页面 JS/meta 重定向到死主机、或两次工具
        调用之间导航以错误页提交）必须报 ``navigation_failed``：真相是网络失败，交给 ``validate``
        会变成“安全策略拒绝”（C3b 修过动作后复查，入口这条路径漏了一次）；``about:neterror``
        同样被先认出来，不再被空白页判定静默放行。

        放行的两种：URL 读不出来（页面已关/驱动异常，交给 closed-page 恢复路径）与判定为
        ``_URL_OK``（空白页 / 策略内）。拦截：``navigation_failed`` / ``navigation_blocked``
        + **弃用该页**（同 I2：错误页与违规源都不回 idle）。
        """
        url = _page_url(page)
        if not url:
            return None
        verdict, reason = self._classify_page_url(url)
        if verdict == _URL_OK:
            return None
        await self._discard_page(page)
        if verdict == _URL_ERROR_PAGE:
            return _browser_error(
                "navigation_failed",
                f"当前页面停在浏览器错误页（导航失败）：{_preview(url)}",
                data=_verdict_data(action, url, reason),
            )
        return _browser_error(
            "navigation_blocked",
            f"当前页面停在策略不允许的 URL：{reason}",
            data=_verdict_data(action, url, reason),
        )

    def _classify_page_url(self, url: str) -> tuple[str, str]:
        """当前页 URL 的复查判定（F1）：``(档位, 原因)``，``(_URL_OK, "")`` = 放行。

        **顺序即优先级**，入口守卫与动作后复查共用这一个 helper——两处各自维护顺序必然漂移
        （C3b 修过动作后复查，入口守卫又漏了一次）：

        1. 浏览器错误页（``chrome-error://…`` / ``about:neterror``）→ ``_URL_ERROR_PAGE``：
           它的 scheme 不在白名单，先跑 ``validate`` 会把真实网络故障（DNS 失败 / 连接被拒，
           P4 高频）报成“安全策略拒绝”（错误归因全错）；``about:neterror`` 更会被空白页判定
           静默放行。
        2. 空白页（``about:blank``，新建页面的初始状态）→ 放行：不是跳转，也不是违规。
        3. 其余交给策略 ``validate``。
        """
        if _is_browser_error_url(url):
            return _URL_ERROR_PAGE, "browser_error_page"
        if _is_blank_url(url):
            return _URL_OK, ""
        allowed, reason = self._policy.validate(url)
        return (_URL_OK, "") if allowed else (_URL_BLOCKED, reason)

    async def _revalidate_after_action(
        self, page: Page, action: str, result: ToolResult
    ) -> ToolResult:
        """动作后复查当前 URL（I2）：入参把关挡不住“动作把页面带出去”（链接/弹窗/重定向/JS 跳转）。

        判定用 :meth:`_classify_page_url`（与入口守卫**共用同一套顺序**，F1）：

        1. URL 读不出来（页面已关/驱动异常）→ 原结果返回：那不是"跳到了违规地址"，
           交给 closed-page 恢复路径（:meth:`_current_page`）。
        2. ``_URL_ERROR_PAGE``（浏览器错误页 ``chrome-error://chromewebdata/`` / ``about:neterror``）
           → **导航失败**（C3b）：DNS 失败/连接被拒绝（P4 高频）的页面就停在这里，scheme 不在
           白名单 —— 直接交给策略会把真实网络故障记成"安全策略拦截"（错误归因全错）。
           按导航失败如实上报 + **弃用**（错误页不复用，同 F4），不 release、不 quarantine。
        3. ``_URL_OK``（空白页 / 策略内）→ 原结果原样返回：动作本身的结果才是数据。
        4. ``_URL_BLOCKED`` → ``navigation_blocked`` + **弃用当前页**：后续调用不会继续在违规源上
           动作，必须先重新 ``browser_open`` 过策略门（永远不回 idle：归还等于把违规源交给下个消费者）。
        """
        url = _page_url(page)
        if not url:
            return result
        verdict, reason = self._classify_page_url(url)
        if verdict == _URL_OK:
            return result
        await self._discard_page(page)
        if verdict == _URL_ERROR_PAGE:
            return _browser_error(
                "navigation_failed",
                f"动作后页面停在浏览器错误页（导航失败）：{_preview(url)}",
                data=_verdict_data(action, url, reason),
            )
        return _browser_error(
            "navigation_blocked",
            f"动作后页面跳转到策略不允许的 URL：{reason}",
            data=_verdict_data(action, url, reason),
        )

    async def _discard_page(self, page: Page | None) -> None:
        """弃用页面并丢掉当前页引用（best-effort，绝不覆盖动作结果）：两类页面走这里。

        - **失败导航后的浏览器错误页**（F4）：错误页自身的导航可能还在飞，复用同一页的下一次
          ``goto`` 会被它打断（T6 冒烟 S6 确定性复现的假失败）。
        - **跳出策略的违规页**（I2）：违规源既不能继续动作，也不能归还池——归还的页面会被下一个
          ``new_page()`` 原样取走，等于把违规源（以及它的登录态/内容）交给别的消费者。

        两者都只能**关掉**（``discard()``，不回 idle）。代价：失败后不能再 snapshot 那个错误页
        （本就没有信息量，错误内容已经在 ``error_message`` 里）。
        best-effort：池不可用 / 替身没实现 discard 时只记日志，绝不改变动作结果。
        """
        if page is None:
            return
        self._forget_page(page)
        if self._browser is None:
            return
        try:
            await self._browser.discard(page)
        except Exception as exc:
            logger.debug("page discard failed: {}", _describe(exc))

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
        """释放本 executor 借出的**全部**页面（跨上下文；幂等）：不调则那些池槽一直占着。

        P5 起"当前页"是上下文局部的，而 ``aclose()`` 由**外层任务**调用（评测 harness 的逐场景
        teardown；页面则是图节点上下文取的）——只归还"本上下文的当前页"等于什么都不归还，
        C2③ 的逐场景隔离就成了空话。所以这里按**借出记录**（``_held_pages``）归还：本 executor
        从池里拿过、既没弃用也没归还的页，一个不落。

        残余限制（与模块 docstring 同源）：归还的页面仍停在最后打开的 URL 上，多会话并发时
        别的消费者可能从 idle 取到它；正在使用该页的上下文不会被叫停（页引用已归还，但只有池
        的 idle 复用才算“换了手”——归还后本 executor 不会再在旧页上动作）。尚未接到 app
        lifespan（P4 议题）。
        """
        pages, self._held_pages = self._held_pages, []
        self._page_var.set(None)
        for page in pages:
            await self._release_page(page)

    async def _acquire_page(self) -> Page:
        """取当前页（本上下文首次向池索取并记住）；池不可用 → :class:`_PoolUnavailableError`。

        ``_page_lock`` 保护这里的"检查-取页"对（**池操作**）：同一上下文里并发调用不会各自
        ``new_page()``（那会突破池的 ``max_pages``）。不同上下文（会话/任务）**各自取页**，正是
        P5 的隔离目标。顺带清一次借出记录里的死页（池回收过的页不该一直被引用拦着，见
        :meth:`_forget_page`）。
        """
        pool = self._browser
        if pool is None:  # _call_browser 已拦住；这里只是给类型收窄
            raise _PoolUnavailableError(_BROWSER_MISSING_MSG)
        async with self._page_lock:
            existing = self._current_page()
            if existing is not None:
                return existing
            self._held_pages = [held for held in self._held_pages if not _page_closed(held)]
            try:
                page = await pool.new_page()
            except Exception as exc:  # 未 start() / 启动失败 / 浏览器崩了
                raise _PoolUnavailableError(
                    f"BrowserPool.new_page() 失败（{_describe(exc)}）"
                ) from exc
            self._page_var.set(page)
            self._held_pages.append(page)
            return page


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


def _navigation_failure(result: ToolResult) -> ToolResult:
    """导航工具的失败归因归一化（C3b：错误要如实归因，不能张冠李戴）。

    ``actions.navigate`` 只按**异常类型**分档，而 Playwright 把网络级失败
    （``net::ERR_NAME_NOT_RESOLVED`` / ``net::ERR_CONNECTION_REFUSED`` …，P4 高频）统一抛成
    ``playwright.async_api.Error`` → 归成笼统的 ``playwright_error``。但 browser_open 这一步
    **只可能**是导航失败：DNS 解析不了/连接被拒必须留在 ``navigation_failed`` 这一档，
    P4 才能把"页面打不开"与"浏览器/驱动坏了"分开（否则错误归因全错）。
    原始档位保留在 ``data["driver_error_type"]``（诊断不丢）；更具体的档位
    （``invalid_url`` / ``timeout`` / ``navigation_blocked``）原样保留。
    """
    if result.status == "success" or result.error_type != "playwright_error":
        return result
    return ToolResult(
        status="error",
        content=result.content,
        error_type="navigation_failed",
        error_message=result.error_message,
        data={**result.data, "driver_error_type": result.error_type},
    )


#: 可以“提升成成功”的点击失败档（F2）：只有 timeout 是“等待被导航打断”的形状。
_PROMOTABLE_CLICK_ERROR_TYPE = "timeout"


def _url_without_fragment(url: str) -> str:
    """URL 去掉 ``#fragment``（F2）：``#`` 之后的变化是**页内锚点**，不是导航。

    ``https://x/p`` → ``https://x/p#reviews`` 只换了页内位置，页面本身没变：
    点击等待就算被打断也不代表“点击生效了”。只比 ``#`` 之前的部分才是“页面有没有换”。
    """
    return urlunsplit(urlsplit(url)._replace(fragment=""))


def _promote_click_that_navigated(
    result: ToolResult, url_before: str, page: Page
) -> ToolResult:
    """点击失败但页面真的跳走了 → 按“已生效”返回成功（C3a）。

    T6 冒烟实测（跨页点击假失败 1/2）：点击触发导航时 Playwright 的 ``click`` 等待会被导航
    打断并报 timeout，而动作其实已经生效（URL 已变）——把“已生效”记成失败会把 P4 的成功任务
    算成失败。所以失败路径对比动作前的 URL。**三道门全过才算导航**（F2 收窄，旧实现只看
    “URL 字符串变了”）：

    1. ``error_type == "timeout"``：只有它是“等待被导航打断”的形状。``invalid_target`` /
       ``action_failed`` / ``playwright_error`` 是别的原因，这些失败路径上 URL 变了也不构成
       “点击生效”的证据（可能是页面自己跳走的），按成功交出就是假成功。
    2. 页面真的换了：只比 ``#`` 之前的部分（:func:`_url_without_fragment`）——``/start`` →
       ``/start#reviews`` 是同页锚点，不是导航。两侧 URL 都要读得出来：读不出来就没有“变”的
       基准，不提升。
    3. 新地址不是浏览器错误页 / 空白页：要么导航失败要么没有落点，交给复查看。
       尤其不能提升成成功：那会把“点了一个打不开的链接”记成任务成功。

    没变 / 读不出来 / 命中错误页与空白页 → 原错误（失败是真的）。真的换了页面 → 成功 +
    ``data["navigated"]=True``；原错误原样保留在 ``data["error_before_navigation"]``（诊断不丢）。

    复查（:meth:`AppToolExecutor._revalidate_after_action`）照常跟在后面：跳到的地址仍要过策略。
    """
    if result.status == "success" or result.error_type != _PROMOTABLE_CLICK_ERROR_TYPE:
        return result
    url_after = _page_url(page)
    if not url_after or not url_before:
        return result
    if _url_without_fragment(url_after) == _url_without_fragment(url_before):
        return result
    if _is_browser_error_url(url_after) or _is_blank_url(url_after):
        return result
    label = str(result.data.get("target") or "目标")
    return ToolResult(
        status="success",
        content=f"已点击 {label}（点击触发的导航打断了等待，动作已生效）",
        data={
            **result.data,
            "navigated": True,
            "url_before": url_before,
            "url": url_after,
            "error_before_navigation": {
                "error_type": result.error_type,
                "error_message": result.error_message,
            },
        },
    )


#: 当前页 URL 复查的三档判定（F1）：入口守卫与动作后复查共用（顺序即优先级）。
_URL_OK = "ok"
_URL_ERROR_PAGE = "error_page"
_URL_BLOCKED = "blocked"


#: 浏览器错误页的 scheme/host 前缀（F5）：判定只认前缀，绝不子串匹配。
_BROWSER_ERROR_PREFIXES = (
    "chrome-error:",  # Chromium：chrome-error://chromewebdata/
    "edge-error:",  # Edge：edge-error://edgewebdata/
    "chrome://network-error",  # 手输/内部导航进的网络错误页
    "about:neterror",  # about:neterror?e=dnsNotFound（页面 JS/meta 重定向常见落点）
)


def _is_browser_error_url(url: str) -> bool:
    """浏览器错误页（导航失败）的 URL（C3b / F5）：``chrome-error://chromewebdata/`` 等。

    自己的 scheme 不在策略白名单里，直接交给 ``validate`` 会被记成“安全策略拦截” ——
    而真实原因是 DNS 解析失败 / 连接被拒绝（P4 高频）：错误归因不能错。

    判定只能按 **scheme/host 前缀**（F5）：``"neterror" in url`` / ``"chromewebdata" in url``
    这类子串判定会把合法地址（``https://example.com/neterror``、``https://chromewebdata.example.com/``）
    当成错误页 —— 成功动作后的复查据此把结果替换成 ``navigation_failed`` 并 discard **活页**
    （页面内容/登录态一起丢，真实数据丢失）。四种写法（Chromium / Edge / 内部网络错误页 /
    ``about:neterror``）都按前缀认。
    """
    return url.strip().lower().startswith(_BROWSER_ERROR_PREFIXES)


def _is_blank_url(url: str) -> bool:
    """空白页（``about:blank`` 等浏览器内部页）—— 新建页面的**初始状态**，不是"跳到了哪里"。

    两种误判都要求它是空白：入口/动作后复查把它当成策略违规（假）拦下来；
    或把它当"已导航"（页面其实什么都没加载）。
    """
    return url.strip().lower().startswith("about:")


def _verdict_data(action: str, url: str, reason: str) -> dict[str, Any]:
    """复查判定命中时的 ``data`` 形状（F1）：两个复查点完全一致（只差文案）。"""
    return {"action": action, "url": _preview(url), "reason": reason, "page_discarded": True}


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
    """页面**渲染后可见文本**采样：``body.inner_text()`` 前 ``detect.MAX_HTML_SAMPLE_CHARS``
    字符、小写；读不出当空串。

    为什么不是 ``page.content()``（原始 HTML，F1 / T5 审查）：关键词规则判的是**用户看得见什么**
    （登录表单的「账号/密码」、验证码文案、确认弹窗文案），而原始 HTML 里还躺着用户看不见的东西
    ——``display:none`` 的隐藏登录下拉、``<script>`` / i18n bundle（里面也有「验证码」这类词条）。
    它们出现在源码里不代表页面上有对应物，而误报要花掉每次运行唯一的人工暂停机会（实例：电商
    产品页的可见「登录」导航链接 + 隐藏登录下拉 → 旧接线误报 HITL）。

    ``inner_text`` 只返回渲染可见文本：``display:none`` 子树与 ``<script>/<style>`` 都不在内。
    代价（有意接受）：可见文本里没有标签，所以**标签级**结构标记（``role="dialog"``）不再可用，
    确认弹窗只能用短语文案判定（见 :mod:`browsepilot.app.browser.detect`）；密码框那条结构探针
    不受影响（:func:`_has_password_field` 用 ``:visible`` 直接问 DOM）。

    命名沿用 D5/D6 的接线契约（``html_sample``）：它现在装的是可见文本，不再是 HTML 源码。
    """
    try:
        text = await page.locator("body").inner_text(timeout=_SAMPLE_TIMEOUT_MS)
    except Exception as exc:  # 页面已关 / 驱动异常 / 没有 body：检测降级，不影响动作结果
        logger.debug("detector: body.inner_text() failed: {}", _describe(exc))
        return ""
    if not isinstance(text, str):
        return ""
    limit = detect.MAX_HTML_SAMPLE_CHARS
    return text[:limit].lower()


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
