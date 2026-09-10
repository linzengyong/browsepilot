"""P3-D6 登录/验证码启发式的纯函数单测（不碰浏览器、不起服务）。

``detect_intervention`` 是 HITL 桥的判定核心：命中意味着 agent 会暂停等人工
（``graph.execute`` 见 ``data.needs_human`` 就转 hitl 节点 interrupt），所以这里既测
「该命中的都命中」，也测**不该命中的绝不命中**——误报的代价是没必要的人工暂停，而且每次
运行只有一次暂停机会（``graph._route_execute_result`` 在 ``hitl_triggered_count > 0`` 后不再中断，
一次误报会把唯一的机会用掉）。

覆盖：brief 的两个样例 / URL 标记（路径段 + 主机首标签，含负例）/ 密码框 / 验证码短语与品牌词 /
中文关键词组（入口词 + 凭据词；单个关键词不算）/ 确认弹窗 / 优先级 / 20KB 上限 /
脏输入不抛异常 / 纯标准库（可离线、零页面往返）。

F1（T5 审查）之后的契约：``html_sample`` 装的是**渲染后可见文本**（executor 的 ``_sample_html``
用 ``body.inner_text()``，隐藏子树 / ``<script>`` / i18n bundle 都不在内），所以 ``dialog``
判定只用短语文案（标签级结构标记不可用）；隐藏 DOM 的误报在接线层修，见
tests/test_app/test_executor_browser.py 的 F1 用例。
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from browsepilot.app.browser import detect
from browsepilot.app.browser.detect import (
    MAX_HTML_SAMPLE_CHARS,
    Intervention,
    detect_intervention,
)

NORMAL_HTML = "<html><body><h1>一篇普通的文章</h1><p>没有任何需要人工处理的标记。</p></body></html>"


# ── brief 的两个样例 ────────────────────────────────────────────────────────


def test_detect_login_page():
    """brief 样例 1：带密码框的 /login 页面 → login。"""
    r = detect_intervention(url="https://x.com/login", html_sample="", has_password=True)
    assert r is not None
    assert r.kind == "login"
    assert r.confidence == "high"
    assert r.human_prompt.startswith("需要人工处理：")
    assert "登录" in r.human_prompt


def test_detect_captcha_keywords():
    """brief 样例 2：无登录特征的结账页出现验证码文案 → captcha。"""
    r = detect_intervention(
        url="https://x.com/checkout",
        html_sample="请输入验证码 slide to verify",
        has_password=False,
    )
    assert r is not None
    assert r.kind == "captcha"
    assert "验证" in r.human_prompt


# ── 边界负例：普通页面必须为 None ───────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/article/42",
        "https://example.com/products/12345?utm_source=newsletter",
        "",
    ],
)
def test_normal_pages_are_not_interventions(url):
    assert detect_intervention(url=url, html_sample=NORMAL_HTML, has_password=False) is None


def test_dirty_inputs_never_raise():
    """检测是 best-effort：脏输入（None / 非 str / 非法 URL）只换来确定性的 None。"""
    assert detect_intervention(None, None, False) is None  # type: ignore[arg-type]
    assert detect_intervention("http://[::1", "<p>普通页面</p>", False) is None
    assert detect_intervention("https://x.com/p", 12345, False) is None  # type: ignore[arg-type]


# ── login：URL 标记 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/login",
        "https://x.com/Login",  # 站点常把路径首字母写大写
        "https://x.com/login.html",  # 老站点带扩展名
        "https://x.com/account/login?next=/orders",  # query 不影响判定
        "https://x.com/user/login/",
        "https://x.com/users/sign_in",
        "https://x.com/sign-in/verify",
        "https://x.com/v2/logon",
        "https://signin.example.com/oauth2/authorize",  # SSO：路径里没有 login，靠主机首标签
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    ],
)
def test_login_url_markers_are_high_confidence(url):
    r = detect_intervention(url=url, html_sample="", has_password=False)
    assert r is not None
    assert r.kind == "login"
    assert r.confidence == "high"
    assert r.reason.startswith(("url-path:", "url-host:"))


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/logout",
        "https://x.com/logins",  # 更长/复数词不是登录页
        "https://x.com/products/login-tips",  # 段内出现 login，但不是登录页（博客/导购页常见）
        "https://x.com/blog/how-to-sign-in",
        "https://x.com/articles/trouble-login-device",
        "https://x.com/?redirect=/login",  # query 说的是「要去哪」，不是「现在在哪」
        "https://app.example.com/#/login",  # fragment 可能只是文档锚点 → 有意漏报（见报告）
    ],
)
def test_non_login_urls_are_not_markers(url):
    assert detect_intervention(url=url, html_sample="", has_password=False) is None


# ── login：密码框 ───────────────────────────────────────────────────────────


def test_password_field_alone_is_login():
    """brief 边界条件：``has_password`` 单独成立即 login（结构标记，无需任何文本）。"""
    r = detect_intervention(url="https://example.com/secure", html_sample=NORMAL_HTML, has_password=True)
    assert r is not None
    assert r.kind == "login"
    assert r.confidence == "high"
    assert r.reason == "password-input"


# ── captcha：短语 / 品牌词 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "marker",
    [
        "验证码",
        "captcha",
        "recaptcha",
        "verify you are human",
        "slide to verify",
        "人机验证",
        "滑动验证",
        "点击验证",
        "滑块",
    ],
)
def test_captcha_marker_alone_is_captcha(marker):
    """brief 边界条件：验证码关键词单独成立即 captcha（不需要登录特征）。"""
    r = detect_intervention(
        url="https://example.com/p", html_sample=f"<p>{marker}</p>", has_password=False
    )
    assert r is not None
    assert r.kind == "captcha"
    assert r.reason.startswith(("captcha-keyword:", "captcha-brand:"))
    assert "验证" in r.human_prompt


@pytest.mark.parametrize(
    "text",
    [
        "I'm not a robot",  # 源码里的原文
        "I&#39;m not a robot",  # HTML 转义后的撇号
        "I am not a robot",
        "我不是机器人",
    ],
)
def test_not_a_robot_variants(text):
    r = detect_intervention(url="https://example.com/p", html_sample=text, has_password=False)
    assert r is not None
    assert r.kind == "captcha"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<p>请输入验证码</p>", "high"),  # 明确的挑战短语
        ('<script src="https://www.google.com/recaptcha/api.js"></script>', "medium"),
        # 品牌词可能只是隐形 reCAPTCHA v3 的脚本引入（页面上可能根本没有可见验证码）
    ],
)
def test_captcha_confidence_distinguishes_phrase_from_brand(text, expected):
    r = detect_intervention(url="https://example.com/p", html_sample=text, has_password=False)
    assert r is not None
    assert r.kind == "captcha"
    assert r.confidence == expected


def test_html_matching_is_case_insensitive():
    r = detect_intervention(
        url="https://example.com/p", html_sample="<P>VERIFY YOU ARE HUMAN</P>", has_password=False
    )
    assert r is not None
    assert r.kind == "captcha"


# ── login：中文关键词组 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "<p>请登录</p><p>密码</p>",
        "<p>登录</p><p>账号</p><p>密码</p>",
        "<p>登入 密碼</p>",  # 繁体
        "<p>登陆 账户</p>",  # 异体
    ],
)
def test_chinese_login_keywords_need_entry_and_credential(text):
    """入口词 + 凭据词同时出现 → login（medium：文本标记，无结构证据）。"""
    r = detect_intervention(url="https://x.com/p", html_sample=text, has_password=False)
    assert r is not None
    assert r.kind == "login"
    assert r.confidence == "medium"
    assert r.reason.startswith("keywords:")
    assert "登录" in r.human_prompt


@pytest.mark.parametrize(
    "text",
    [
        "<p>请登录</p>",  # 只有入口词，没有凭据词 → 不算
        "<a>登录</a>",  # 导航栏的「登录」链接：中文站几乎每页都有 → 单独不算
        "<label>密码</label>",  # 单独的「密码」也可能是安全说明/帮助页
        "<p>账号 密码</p>",  # 有凭据词但没入口词：找回密码/客服帮助页会这么写
        "<p>账号</p><p>密码</p>",
        "<li>修改密码</li><li>绑定账号</li>",
        "<p>Sign in</p><p>Password</p>",  # 英文关键词不在启发式里（有密码框时由 has_password 兜住）
    ],
)
def test_weak_text_markers_alone_do_not_fire(text):
    assert detect_intervention(url="https://x.com/p", html_sample=text, has_password=False) is None


# ── 优先级：结构标记 > 验证码 > 文本关键词 > 弹窗 ────────────────────────────


def test_structural_login_wins_over_captcha_text():
    """登录页常常自带验证码：此时人工要做的是「登录」（验证码只是其中一步）。"""
    r = detect_intervention(
        url="https://x.com/login", html_sample="<p>请输入验证码</p>", has_password=False
    )
    assert r is not None
    assert r.kind == "login"


def test_password_input_wins_over_captcha_script_include():
    """隐形 reCAPTCHA v3 的脚本引入不等于要人工：有密码框就是登录页。"""
    r = detect_intervention(
        url="https://x.com/auth",
        html_sample='<script src="https://www.google.com/recaptcha/api.js"></script>',
        has_password=True,
    )
    assert r is not None
    assert r.kind == "login"


def test_captcha_wins_over_keywords_and_dialog():
    r = detect_intervention(
        url="https://x.com/p",
        html_sample="<p>请确认 登录 密码 人机验证</p>",
        has_password=False,
    )
    assert r is not None
    assert r.kind == "captcha"


def test_keyword_login_wins_over_dialog():
    r = detect_intervention(
        url="https://x.com/p", html_sample="<p>登录 密码</p><p>如需继续请点击</p>", has_password=False
    )
    assert r is not None
    assert r.kind == "login"


# ── dialog：确认弹窗（可选启发式，最弱的信号） ──────────────────────────────


@pytest.mark.parametrize(
    "text", ["<p>确认对话框</p>", "<p>如需继续请点击</p>", "<p>请点击确认</p>", "<p>请确认后继续</p>"]
)
def test_dialog_markers(text):
    r = detect_intervention(url="https://x.com/p", html_sample=text, has_password=False)
    assert r is not None
    assert r.kind == "dialog"
    assert r.confidence == "medium"
    assert "确认" in r.human_prompt


def test_hidden_login_markup_is_not_in_the_sample_contract():
    """F1（T5 审查）：电商产品页的可见「登录」导航链接 + ``display:none`` 的「账号/密码」下拉。

    隐藏子树与 ``<script>``/i18n bundle 都在原始 HTML 里、都在用户看不见的地方——而
    ``html_sample`` 的契约输入是**渲染后可见文本**（``body.inner_text()``；接线见 executor 的
    ``_sample_html``）。可见文本里只剩导航栏的入口词：只有入口词、没有凭据词 → ``None``。
    """
    assert (
        detect_intervention(
            url="https://x.com/product/42", html_sample="首页 登录 购物车", has_password=False
        )
        is None
    )


def test_visible_captcha_text_still_fires():
    """F1 对照组：页面上看得见的验证码文案（用户真要处理的东西）照样命中。"""
    r = detect_intervention(
        url="https://x.com/checkout", html_sample="请完成人机验证后继续", has_password=False
    )
    assert r is not None
    assert r.kind == "captcha"


def test_generic_confirm_alone_is_not_a_marker():
    """「请确认」出现在普通下单/地址确认页里的概率太高，单独不算。

    F1 连带调整：旧实现靠「``请确认`` + ``role="dialog"`` 模态结构」补足特异性，而模态结构是
    **标签级**标记——采样换成渲染后可见文本后标签不在输入里，这条组合规则随之去掉：可见
    文本里只剩文案，特异性只能由**具体短语**提供（见 :data:`_DIALOG_PHRASE_MARKERS`）。
    """
    assert (
        detect_intervention(
            url="https://x.com/checkout",
            html_sample="请确认收货地址 请选择配送时间",
            has_password=False,
        )
        is None
    )


# ── 采样上限 / 值对象 / 依赖面 ──────────────────────────────────────────────


def test_only_the_first_20kb_of_html_is_inspected():
    """上限两侧的行为都要确定：前 20KB 命中即可，超出的部分一律不看（O(20KB) 保证）。"""
    beyond = "x" * MAX_HTML_SAMPLE_CHARS + "<p>验证码</p>"
    assert detect_intervention(url="https://x.com/p", html_sample=beyond, has_password=False) is None
    within = "<p>验证码</p>" + "x" * (MAX_HTML_SAMPLE_CHARS * 2)
    hit = detect_intervention(url="https://x.com/p", html_sample=within, has_password=False)
    assert hit is not None and hit.kind == "captcha"


def test_intervention_is_a_frozen_value_object():
    r = detect_intervention(url="https://x.com/login", html_sample="", has_password=False)
    assert isinstance(r, Intervention)
    with pytest.raises(FrozenInstanceError):
        r.kind = "captcha"  # type: ignore[misc]


def test_detect_module_is_stdlib_only():
    """D6：检测不 import playwright / 不 import agent 层——可离线单测、零页面往返、零 LLM。"""
    tree = ast.parse(Path(detect.__file__).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert modules <= {"__future__", "dataclasses", "typing", "urllib.parse"}, modules


def test_prompts_are_ready_to_show():
    """提示语直接进 interrupt 载荷给用户看：必须是完整中文句子（不是标记名/英文）。"""
    cases = {
        "login": detect_intervention(url="https://x.com/login", html_sample="", has_password=False),
        "captcha": detect_intervention(
            url="https://x.com/p", html_sample="<p>验证码</p>", has_password=False
        ),
        "dialog": detect_intervention(
            url="https://x.com/p", html_sample="<p>确认对话框</p>", has_password=False
        ),
    }
    for kind, intervention in cases.items():
        assert intervention is not None and intervention.kind == kind
        assert intervention.human_prompt.startswith("需要人工处理：")
        assert intervention.human_prompt.endswith("。")
        assert "\n" not in intervention.human_prompt
