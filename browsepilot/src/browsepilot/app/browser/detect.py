"""登录/验证码/确认弹窗启发式（P3-D5/D6）：纯函数、零 LLM、零页面往返。

Tier3 的输入是「浏览器动作之后的页面」：打开/点击/输入之后，页面可能停在登录墙、
人机验证或确认弹窗上——此时自动化的每一步都会失败或者原地打转，正确做法是暂停让
人工处理（P2 的 HITL 机制）。本模块只回答一个问题：**这个页面需要人吗？**

设计约束与取舍：

- **零页面往返**：所有输入都是调用方已经拿到的字符串/布尔（``url`` = 当前页 URL，
  ``html_sample`` = ``Page.content()`` 的前 ``MAX_HTML_SAMPLE_CHARS`` 个字符，
  ``has_password`` = 调用方对密码输入框的探测结果）。本模块不 import playwright、
  不解析 DOM、不发网络请求、不用 LLM。
- **保守优先**：误报的代价是「没必要的人工暂停」——而且每次运行只有一次暂停机会
  （``graph._route_execute_result`` 只在 ``hitl_triggered_count == 0`` 时转 hitl 节点），
  一次误报就会把唯一机会用掉；漏报的代价只是继续走既有降级链（诚实汇报失败）。
  所以只有**强标记**才命中：URL 登录路径/主机、密码输入框、验证码短语，以及
  「入口词 + 凭据词」同时出现的中文关键词组（单个「登录」不算：中文站的导航栏
  几乎每页都有登录链接）。
- **可解释**：命中附带 :attr:`Intervention.reason`（命中的具体标记），trace 与单测
  都能定位是哪条规则触发的。

判定优先级（:func:`detect_intervention`）：URL 登录标记 → 密码框 → 验证码 →
中文关键词组 → 确认弹窗。「先结构、后文本」的理由：``/login`` 与
``<input type=password>`` 几乎只在登录/改密场景出现，而登录页**常常自带验证码**——
此时人工要做的是「登录」（验证码只是其中一步），所以结构性登录证据排在验证码之前。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

__all__ = ["MAX_HTML_SAMPLE_CHARS", "Intervention", "detect_intervention"]

MAX_HTML_SAMPLE_CHARS = 20_000
"""只检查 HTML 采样的前 2 万字符（executor 也按这个上限截断）。

登录/验证码是**首屏**特征，前 20KB 足够；上限同时保证本函数对任意长度的输入都是
有界的 O(20KB)：页面再大也不会把检测成本变成整页扫描。
"""

_LOGIN_PROMPT = "需要人工处理：检测到登录页面，请在浏览器中手动登录后继续任务。"
_CAPTCHA_PROMPT = "需要人工处理：检测到验证码/人机验证，请手动完成验证后继续任务。"
_DIALOG_PROMPT = "需要人工处理：页面需要手动确认，请处理后继续任务。"

# ── 标记表（顺序 = 特异性从高到低，reason 会回显第一个命中的标记） ──────────────

_LOGIN_PATH_SEGMENTS = frozenset({"login", "log-in", "signin", "sign-in", "sign_in", "logon"})
"""登录页的路径段（``/login``、``/users/sign_in``）：整段精确匹配，不做子串匹配。"""

_LOGIN_HOST_LABELS = frozenset({"login", "signin", "sign-in"})
"""登录页的主机首标签（``login.microsoftonline.com``、``signin.aws.amazon.com``）。

SSO/OAuth 的登录页路径里往往没有 ``login``（``/common/oauth2/v2.0/authorize``），
首标签是唯一稳定标记；精确相等意味着 ``loginradius.com``、``blog.example.com`` 不会误判。
"""

_PATH_EXTENSIONS = frozenset({"htm", "html", "php", "asp", "aspx", "jsp", "do", "action"})
"""老站点会在登录段后面带扩展名（``login.php``）：判定前剥掉，避免漏报。"""

_LOGIN_ENTRY_KEYWORDS = ("登录", "登入", "登陆")
"""登录「入口词」（含繁体与常见异体「登陆」）。"""

_LOGIN_CREDENTIAL_KEYWORDS = (
    "密码",
    "密碼",
    "账号",
    "帐号",
    "帳號",
    "账户",
    "帳戶",
    "賬號",
)
"""登录「凭据词」（含繁体/异体）。入口词与凭据词**同时**出现才算登录页。"""

_CAPTCHA_PHRASE_MARKERS = (
    "verify you are human",
    "not a robot",
    "不是机器人",
    "slide to verify",
    "人机验证",
    "滑动验证",
    "点击验证",
    "验证码",
    "滑块",
)
"""明确的「要人来做验证」短语（含 ``I'm not a robot`` / ``I&#39;m not a robot`` 等撇号变体）。"""

_CAPTCHA_BRAND_MARKERS = ("captcha",)
"""验证码品牌词（覆盖 ``recaptcha`` / ``hcaptcha``）：可能只是隐形 reCAPTCHA v3 的脚本引入
（页面上根本没有可见验证码），所以单独命中只给 ``medium`` 置信度。"""

_DIALOG_PHRASE_MARKERS = ("确认对话框", "如需继续请点击", "请点击确认", "请确认后继续")
"""确认弹窗的具体文案。"""

_DIALOG_MODAL_MARKERS = ('aria-modal="true"', 'role="dialog"', "role='dialog'")
"""「当前有打开的模态框」的结构标记（框架只在弹窗**打开时**才写 ``aria-modal``）。"""

_DIALOG_GENERIC_MARKER = "请确认"
"""泛化的确认文案：普通下单页/地址页也会写「请确认…」，必须配模态结构才算弹窗。"""


@dataclass(frozen=True)
class Intervention:
    """一次「需要人工介入」的判定结果（executor 取 ``kind``/``human_prompt`` 上 HITL 桥）。

    - ``kind``：``login`` / ``captcha`` / ``dialog``（进 HITL 载荷的 ``intervention`` 字段）。
    - ``confidence``：``high`` = 结构性强标记（登录 URL、密码框、验证码短语）；
      ``medium`` = 文本弱标记（中文关键词组、验证码品牌词、确认弹窗）。
      只用于诊断与文案分级——**是否暂停由「有没有命中」决定，不由置信度决定**。
    - ``reason``：命中的具体标记（``url-path:login`` / ``password-input`` /
      ``captcha-keyword:验证码`` / ``keywords:登录+密码`` / ``dialog-phrase:确认对话框``），
      供 trace 与测试定位，不进用户文案。
    - ``human_prompt``：可直接展示给用户的中文提示（P2 interrupt 载荷用）。
    """

    kind: Literal["login", "captcha", "dialog"]
    confidence: Literal["high", "medium"]
    reason: str
    human_prompt: str


def detect_intervention(url: str, html_sample: str, has_password: bool) -> Intervention | None:
    """登录/验证码/确认弹窗启发式：命中返回 :class:`Intervention`，否则返回 ``None``。

    判定顺序（先结构、后文本；越靠前的标记越难误报）：

    1. ``login``（high）：URL 路径段/主机首标签命中登录标记，**或**页面有密码输入框。
    2. ``captcha``：明确的验证码短语（high），其次验证码品牌词（medium）。
    3. ``login``（medium）：中文「入口词」与「凭据词」同时出现。
    4. ``dialog``（medium）：确认弹窗文案，或「请确认」+ 模态结构。

    参数容忍脏值（``None`` 当空处理、非 str 先 ``str()``、非法 URL 跳过 URL 判定）：
    检测是 best-effort，绝不因为输入异常而抛错（调用方还要靠它保住动作结果）。
    ``html_sample`` 超过 :data:`MAX_HTML_SAMPLE_CHARS` 的部分一律不看。
    """
    text = "" if html_sample is None else str(html_sample)
    html = text[:MAX_HTML_SAMPLE_CHARS].lower()
    url_text = "" if url is None else str(url)

    url_marker = _login_url_marker(url_text)
    if url_marker is not None:
        return Intervention("login", "high", url_marker, _LOGIN_PROMPT)
    if has_password:
        return Intervention("login", "high", "password-input", _LOGIN_PROMPT)

    marker = _first_marker(html, _CAPTCHA_PHRASE_MARKERS)
    if marker is not None:
        return Intervention("captcha", "high", f"captcha-keyword:{marker}", _CAPTCHA_PROMPT)
    marker = _first_marker(html, _CAPTCHA_BRAND_MARKERS)
    if marker is not None:
        return Intervention("captcha", "medium", f"captcha-brand:{marker}", _CAPTCHA_PROMPT)

    entry = _first_marker(html, _LOGIN_ENTRY_KEYWORDS)
    credential = _first_marker(html, _LOGIN_CREDENTIAL_KEYWORDS)
    if entry is not None and credential is not None:
        return Intervention("login", "medium", f"keywords:{entry}+{credential}", _LOGIN_PROMPT)

    marker = _dialog_marker(html)
    if marker is not None:
        return Intervention("dialog", "medium", marker, _DIALOG_PROMPT)
    return None


def _login_url_marker(url: str) -> str | None:
    """URL 里的登录标记：路径段（``/login``、``/users/sign_in``）或主机首标签（``login.x.com``）。

    只看路径与主机，**不看 query**（``?redirect=/login`` 是「跳去哪」，不是「现在在哪」，
    付款页/文章页常带这种参数，命中即误报），也**不看 fragment**（``#login`` 可能只是
    文档锚点）。两者都是有意为之的漏报：宁可漏，不可误报（See module docstring）。
    """
    try:
        parts = urlsplit(url)
    except ValueError:  # 非法 URL（如残缺的 IPv6 字面量）：跳过 URL 判定，交给其他标记
        return None
    for segment in parts.path.split("/"):
        marker = _segment_marker(segment)
        if marker is not None:
            return f"url-path:{marker}"
    host = parts.hostname  # 已小写、已剥端口；无主机（相对路径/空 URL）时为 None
    if host:
        label = host.split(".")[0]
        if label in _LOGIN_HOST_LABELS:
            return f"url-host:{label}"
    return None


def _segment_marker(segment: str) -> str | None:
    """路径段归一化后是否是登录标记（``Login`` / ``login.php`` 都算 ``login``）。"""
    text = segment.strip().lower()
    if not text:
        return None
    head, dot, tail = text.rpartition(".")
    if dot and head and tail in _PATH_EXTENSIONS:
        text = head
    return text if text in _LOGIN_PATH_SEGMENTS else None


def _dialog_marker(html: str) -> str | None:
    """确认弹窗标记：具体文案直接命中；泛化的「请确认」需要伴随模态结构。"""
    marker = _first_marker(html, _DIALOG_PHRASE_MARKERS)
    if marker is not None:
        return f"dialog-phrase:{marker}"
    if _DIALOG_GENERIC_MARKER in html:
        modal = _first_marker(html, _DIALOG_MODAL_MARKERS)
        if modal is not None:
            return f"dialog-modal:{_DIALOG_GENERIC_MARKER}+{modal}"
    return None


def _first_marker(text: str, markers: tuple[str, ...]) -> str | None:
    """按声明顺序返回第一个出现在 ``text`` 里的标记（声明序 = 特异性从高到低）。"""
    for marker in markers:
        if marker in text:
            return marker
    return None
