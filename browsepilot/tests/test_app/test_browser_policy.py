r"""Tier3 URL 安全策略单测（D4/D7）：NavigationPolicy 纯函数——无浏览器、无网络、无 DNS。

策略契约（设计决定记录在 ``browsepilot/app/browser/policy.py`` 模块 docstring）：

1. ``allow_https``（默认）：只放行 http/https；``file:``/``javascript:``/``data:``/``ftp:``/
   ``about:``/``chrome:``/``blob:`` 等 scheme 一律拒；字面私有/回环/链路本地/保留地址
   （IPv4+IPv6，含 ``localhost`` 等本地主机名与整数形式 IPv4）一律拒。
2. ``allowlist``：在 scheme 门禁之上，只放行白名单主机（精确匹配或子域后缀匹配）；
   白名单为 ``None``/空/全空白 ⇒ **全拒**（fail-closed，不抛错）——本任务的红线边界。
3. 无效 URL（非字符串、空、缺 scheme、空主机、首尾空白、控制字符、非法 IPv6 括号、
   非法端口、百分号编码主机）⇒ 拒并给出非空原因。
4. 不做 DNS 解析：主机名"在 DNS 上是否指向内网"不在本层职责内（限制已文档化，
   见 ``test_no_dns_resolution_only_literal_host_rules_apply``）。
5. 主机判定按 WHATWG/Chromium 特殊 scheme 语义：``\`` 与 ``/`` 等价（authority 终止符），
   故 ``https://127.0.0.1\@example.com/`` 的真实主机是 ``127.0.0.1`` 而不是 ``example.com``——
   ``urlsplit`` 不这么做，按原文解析会放过 SSRF 载荷（见下文反斜杠用例）。
6. 白名单裸标签条目（无点号，如 ``localhost``）只做**精确**匹配；拒因里回显的主机/scheme
   有长度上限（进状态的数据有界）。

原因串是 T4 面向用户的错误文案来源，故统一为 ``code: detail`` 形态并逐条 pin 住前缀。
"""

from __future__ import annotations

import pytest

from browsepilot.app.browser.policy import (
    REASON_ALLOWLIST_EMPTY,
    REASON_DOMAIN_NOT_ALLOWED,
    REASON_INVALID_URL,
    REASON_PRIVATE_ADDRESS_BLOCKED,
    REASON_PROTOCOL_BLOCKED,
    NavigationPolicy,
)

# ── allow_https：放行面 ────────────────────────────────────────────────────────

PUBLIC_URLS = [
    "https://example.com",
    "http://example.com/a/b?c=d#e",
    "https://sub.deep.example.com:8443/path",
    "https://example.com./",  # FQDN 尾点归一化
    "HTTPS://EXAMPLE.COM/PATH",  # scheme/主机大小写不敏感
    "https://8.8.8.8/",
    "http://93.184.216.34/",
    "https://user:pw@example.com/",  # userinfo 不改变真实主机
    "https://[2001:4860:4860::8888]/",  # 公网 IPv6 字面值
]


@pytest.mark.parametrize("url", PUBLIC_URLS)
def test_allow_https_allows_public_http_and_https(url):
    ok, reason = NavigationPolicy().validate(url)
    assert ok, f"{url} should be allowed, got {reason!r}"
    assert reason == ""


# 172.16-31.x 是私有段，但 172.32.x / 100.63.x / 192.169.x 是公网——防止"偷懒正则"误伤
PUBLIC_EDGE_URLS = [
    "http://172.32.0.1/",
    "http://100.63.0.1/",
    "http://192.169.1.1/",
    "http://11.0.0.1/",
]


@pytest.mark.parametrize("url", PUBLIC_EDGE_URLS)
def test_allow_https_allows_public_neighbours_of_private_ranges(url):
    ok, reason = NavigationPolicy().validate(url)
    assert ok, f"{url} should be allowed, got {reason!r}"


# ── allow_https：危险 scheme ───────────────────────────────────────────────────

DANGEROUS_SCHEMES = [
    "file:///etc/passwd",
    "FILE:///etc/passwd",
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "DATA:TEXT/HTML,X",
    "ftp://example.com/x",
    "ftps://example.com/x",
    "about:blank",
    "chrome://settings",
    "blob:https://example.com/x",
    "vbscript:msgbox(1)",
]


@pytest.mark.parametrize("url", DANGEROUS_SCHEMES)
def test_allow_https_blocks_dangerous_schemes(url):
    ok, reason = NavigationPolicy().validate(url)
    assert not ok, f"{url} must be blocked"
    assert reason.startswith(REASON_PROTOCOL_BLOCKED), reason
    assert len(reason) > len(REASON_PROTOCOL_BLOCKED)  # 带 detail，不是裸 code


def test_scheme_gate_also_applies_in_allowlist_mode():
    # 白名单不能把危险 scheme 放进来（file:///etc/passwd 的 host 为空）
    policy = NavigationPolicy(mode="allowlist", allowed_domains=["example.com"])
    ok, reason = policy.validate("file:///etc/passwd")
    assert not ok
    assert reason.startswith(REASON_PROTOCOL_BLOCKED)


# ── allow_https：私有 / 本机 / 内网字面值 ──────────────────────────────────────

PRIVATE_URLS = [
    # IPv4 私有段
    "http://127.0.0.1/",
    "http://127.1.2.3:8080/x",
    "https://10.0.0.5/x",
    "http://172.16.3.4/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",  # 云元数据端点
    "http://0.0.0.0/",
    "http://255.255.255.255/",
    "http://192.0.2.10/",  # TEST-NET 保留段
    "http://100.64.0.1/",  # CGNAT（is_global=False）
    # IPv6 回环 / 链路本地 / 唯一本地 / 未指定 / v4-mapped
    "http://[::1]/",
    "http://[::]/",
    "http://[fe80::1]/",
    "http://[fd00::1]/",
    "http://[fc00::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[fe80::1%25eth0]/",  # zone id 不能绕过链路本地判定
    # 本地主机名（RFC 6761 localhost / mDNS .local / 私有用途名空间）
    "http://localhost/",
    "http://LOCALHOST:8080/x",
    "http://localhost./",
    "http://api.localhost/",
    "http://printer.local/",
    "http://metadata.google.internal/",
    "http://router.home.arpa/",
    # 单标签主机名（内网命名，公网无此 FQDN）
    "http://intranet/",
    "http://router:8080/admin",
    # 整数 / 八进制 / 十六进制形式 IPv4（Chromium 会解析成 IP）
    "http://2130706433/",
    "http://0177.0.0.1/",
    "http://0x7f.0.0.1/",
]


@pytest.mark.parametrize("url", PRIVATE_URLS)
def test_allow_https_blocks_private_local_targets(url):
    ok, reason = NavigationPolicy().validate(url)
    assert not ok, f"{url} must be blocked"
    assert reason.startswith(REASON_PRIVATE_ADDRESS_BLOCKED), reason


def test_should_block_private_predicate_is_public_api():
    policy = NavigationPolicy()
    assert policy.should_block_private("http://localhost:3000/") is True
    assert policy.should_block_private("https://192.168.0.10/") is True
    assert policy.should_block_private("http://[::1]/") is True
    assert policy.should_block_private("https://example.com/") is False
    assert policy.should_block_private("https://8.8.8.8/") is False
    # 无法解析/无主机 ⇒ 不是"私有"（合法性由 validate 负责），不误报
    assert policy.should_block_private("not a url") is False
    assert policy.should_block_private("") is False


# ── WHATWG 特殊 scheme：``\`` 就是 ``/``（authority 终止符 → 解析器差分 / SSRF）──
#
# ``urlsplit`` 不把 '\' 当 authority 终止符，Chromium/WHATWG 会（已用 node 的 WHATWG
# ``URL`` 对齐复核）。按原文解析会直接放行内网目标：
#   * ``https://127.0.0.1\@example.com/``：原文 host=example.com（allowlist 放行），
#     而 Chromium 访问 ``https://127.0.0.1/@example.com/``；云元数据端点同理；
#   * ``https://169.254.169.254\latest/…``：原文 host 是带反斜杠的怪串 ⇒ 放行。
# 判定主机前必须按 WHATWG 语义把 '\' 归一化成 '/'——交给导航的**原始 URL 不动**。

BACKSLASH_PRIVATE_URLS = [
    "https://127.0.0.1\\foo/",
    "https://169.254.169.254\\latest/meta-data/",  # 云元数据端点
    "https://192.168.1.1\\admin/",
    "https://localhost\\admin/",
    "https://[::1]\\admin/",
    "https://10.0.0.5\\@example.com/",
    "https://127.0.0.1:80\\@example.com/",
    "https://127.0.0.1\\foo\\@example.com/",
]


@pytest.mark.parametrize("url", BACKSLASH_PRIVATE_URLS)
def test_backslash_authority_terminator_cannot_hide_private_hosts(url):
    policy = NavigationPolicy()
    ok, reason = policy.validate(url)
    assert not ok, f"{url} must be blocked: Chromium 的 authority 在反斜杠处终止"
    assert reason.startswith(REASON_PRIVATE_ADDRESS_BLOCKED), reason
    # 独立谓词必须同一套判定（上层复用/展示都读它）
    assert policy.should_block_private(url) is True, url


ALLOWLIST_BACKSLASH_BYPASS_URLS = [
    "https://127.0.0.1\\@example.com/",
    "https://169.254.169.254\\@example.com/",
    "https://localhost\\@example.com/",
    "https://127.0.0.1:80\\@example.com/",
    "https://10.0.0.5\\x\\@example.com/",
    "https://[::1]\\@example.com/",
]


def test_backslash_authority_terminator_cannot_bypass_allowlist():
    r"""原文解析把 ``127.0.0.1\@example.com`` 的 host 读成 example.com ⇒ 白名单放行，
    而 Chromium 访问 127.0.0.1（allowlist 模式的红线）。"""
    policy = NavigationPolicy(mode="allowlist", allowed_domains=["example.com"])
    for url in ALLOWLIST_BACKSLASH_BYPASS_URLS:
        ok, reason = policy.validate(url)
        assert not ok, f"{url} must be blocked: 真实主机在第一个反斜杠之前"
        assert reason.startswith(REASON_DOMAIN_NOT_ALLOWED), reason


def test_backslash_after_the_real_authority_keeps_the_real_host():
    r"""反斜杠只终止 authority、不改变真实主机 ⇒ 不误伤：
    ``https://example.com\path`` 的主机就是 example.com（Chromium 导航到 /path）；
    ``https://example.com\@127.0.0.1`` 与 ``https://example.com/@127.0.0.1/`` 等价。"""
    urls = ("https://example.com\\path", "https://example.com\\@127.0.0.1/")
    policy = NavigationPolicy()
    for url in urls:
        ok, reason = policy.validate(url)
        assert ok, f"{url} should be allowed, got {reason!r}"
        assert policy.should_block_private(url) is False, url
    allowlist = NavigationPolicy(mode="allowlist", allowed_domains=["example.com"])
    for url in urls:
        ok, reason = allowlist.validate(url)
        assert ok, f"{url} should be allowed, got {reason!r}"


@pytest.mark.parametrize("url", ["https:\\\\127.0.0.1/", "https:/127.0.0.1/", "https:127.0.0.1/"])
def test_slash_collapsed_private_targets_fail_closed(url):
    """Chromium 忽略多余斜杠 / 缺 authority 分隔符后照样访问 127.0.0.1；本策略一律拒
    （归一化后按私有地址拒，其余按 invalid_url 拒：宁可误拒不可误放）。"""
    ok, reason = NavigationPolicy().validate(url)
    assert not ok, f"{url} must be rejected, got {reason!r}"


# ── allowlist：精确 + 子域后缀 ────────────────────────────────────────────────


def test_allowlist_allows_exact_and_subdomain():
    policy = NavigationPolicy(mode="allowlist", allowed_domains=["example.com"])
    for url in (
        "https://example.com/x",
        "http://example.com:8080/x",
        "https://www.example.com",
        "https://deep.sub.example.com/a?b=c",
        "https://EXAMPLE.COM./x",  # 大小写 + 尾点归一化
    ):
        ok, reason = policy.validate(url)
        assert ok, f"{url} should be allowed, got {reason!r}"


def test_allowlist_rejects_lookalike_and_unlisted_hosts():
    policy = NavigationPolicy(mode="allowlist", allowed_domains=["example.com"])
    for url in (
        "https://other.com",
        "https://notexample.com/",
        "https://example.com.evil.com/",
        "https://evil.com/?next=example.com",
        "https://example.com@evil.com/",  # userinfo 伪装，真实主机 evil.com
    ):
        ok, reason = policy.validate(url)
        assert not ok, f"{url} must be blocked"
        assert reason.startswith(REASON_DOMAIN_NOT_ALLOWED), reason


def test_allowlist_entries_are_normalized():
    policy = NavigationPolicy(mode="allowlist", allowed_domains=[" Example.COM. ", ".foo.com", "  ", ""])
    for url in ("https://example.com/", "https://www.example.com/", "https://a.foo.com/"):
        assert policy.validate(url)[0], url
    assert not policy.validate("https://bar.com/")[0]


def test_allowlist_wildcards_are_not_supported_and_fail_closed():
    # 设计决定：白名单只接受裸主机名（精确或子域后缀），不支持 '*' 通配。
    # 写成 "*.example.com" 的条目永远不会匹配（fail-closed），不会误放行。
    policy = NavigationPolicy(mode="allowlist", allowed_domains=["*.example.com"])
    for url in ("https://www.example.com/", "https://example.com/"):
        ok, reason = policy.validate(url)
        assert not ok, f"{url} must not match a wildcard entry"
        assert reason.startswith(REASON_DOMAIN_NOT_ALLOWED), reason


def test_allowlist_bare_label_entries_match_exactly_not_as_tld_wildcards():
    """裸标签条目（无点号）只做精确匹配——后缀匹配等于授权整个 TLD：
    ``allowed_domains=["com"]`` 若按 ``host.endswith(".com")`` 匹配会放行 ``evil.com``，
    白名单与 fail-closed 承诺一起失效。"""
    policy = NavigationPolicy(mode="allowlist", allowed_domains=["com"])
    for url in ("https://evil.com/", "https://example.com/", "https://a.b.com/", "https://com.evil/"):
        ok, reason = policy.validate(url)
        assert not ok, f"{url} must not be authorized by the bare-label entry 'com'"
        assert reason.startswith(REASON_DOMAIN_NOT_ALLOWED), reason
    # 精确命中的单标签主机照旧放行（与 localhost 逃逸口同一规则：写哪台主机就只放哪台）
    assert policy.validate("https://com/") == (True, "")


def test_allowlist_bare_label_entry_covers_only_its_exact_host():
    policy = NavigationPolicy(mode="allowlist", allowed_domains=["localhost"])
    assert policy.validate("http://localhost:8099/x")[0]  # 逃逸口本身不受影响
    for url in ("http://api.localhost/", "http://evil.localhost/"):
        ok, reason = policy.validate(url)
        assert not ok, f"{url} must not match the bare-label entry by suffix"
        assert reason.startswith(REASON_DOMAIN_NOT_ALLOWED), reason


def test_allowlist_none_or_empty_rejects_all():
    """红线边界：allowlist 模式缺白名单 ⇒ 全拒（fail-closed），构造不抛错。"""
    for policy in (
        NavigationPolicy(mode="allowlist"),  # allowed_domains=None
        NavigationPolicy(mode="allowlist", allowed_domains=[]),
        NavigationPolicy(mode="allowlist", allowed_domains=["", "  ", "."]),
    ):
        ok, reason = policy.validate("https://example.com/")
        assert not ok, "empty allowlist must deny everything"
        assert reason.startswith(REASON_ALLOWLIST_EMPTY), reason


def test_allowlist_explicit_entry_is_the_escape_hatch_for_local_targets():
    # 设计决定：allowlist 模式的信任边界是"运维显式写下的域名"；
    # 显式写 localhost/127.0.0.1 即视为有意放行本机（私有 IP 拦截只在 allow_https 模式生效）。
    policy = NavigationPolicy(mode="allowlist", allowed_domains=["localhost", "127.0.0.1"])
    for url in ("http://localhost:8099/x", "http://127.0.0.1:8099/"):
        ok, reason = policy.validate(url)
        assert ok, f"{url} explicitly allowlisted, got {reason!r}"


# ── 无效 URL ──────────────────────────────────────────────────────────────────

INVALID_URLS = [
    "",
    "   ",
    "not a url",
    "example.com/path",  # 缺 scheme
    "www.example.com",
    "://example.com",
    "http://",  # 缺主机
    "http:///path",
    "  https://example.com  ",  # 首尾空白
    "https://exa\nmple.com",  # 控制字符（urlsplit 会静默吃掉换行，必须显式拒）
    "https://example.com\u0000/",
    "http://[::1",  # 非法 IPv6 括号
    "https://example.com:notaport/",  # 非法端口
    "http://%31%32%37.0.0.1/",  # 百分号编码主机不能绕过字面 IP 判定
    "https://exa%6dple.com/",
]


@pytest.mark.parametrize("url", INVALID_URLS)
def test_invalid_urls_are_rejected(url):
    ok, reason = NavigationPolicy().validate(url)
    assert not ok, f"{url!r} must be rejected"
    assert reason.startswith(REASON_INVALID_URL), reason


@pytest.mark.parametrize("url", [None, 123, b"https://example.com", ["https://example.com"]])
def test_non_string_input_is_rejected(url):
    ok, reason = NavigationPolicy().validate(url)  # type: ignore[arg-type]
    assert not ok
    assert reason.startswith(REASON_INVALID_URL), reason


def test_reasons_are_stable_prefixed_codes():
    codes = {
        REASON_INVALID_URL,
        REASON_PROTOCOL_BLOCKED,
        REASON_PRIVATE_ADDRESS_BLOCKED,
        REASON_DOMAIN_NOT_ALLOWED,
        REASON_ALLOWLIST_EMPTY,
    }
    assert "" not in codes and all(codes) and len(codes) == 5
    # 拒因必须带 detail（code 之外还有内容），且是 (bool, str) 二元组
    ok, reason = NavigationPolicy().validate("file:///etc/passwd")
    assert (ok, type(reason)) == (False, str)
    assert reason.partition(":")[2].strip(), reason


# ── 状态卫生：拒因里的回显有界（进 agent 状态/ToolResult 的数据一律截断）──────────


def test_rejection_reasons_truncate_long_hosts():
    """主机长度无界 ⇒ 拒因长度无界，而拒因会进入 ToolResult/agent 状态
    （仓库约定：进状态的数据截断，见 actions.py 的 ``_MAX_ERROR_CHARS``）。"""
    long_host = "a" * 5000
    cases = [
        (NavigationPolicy(), f"http://{long_host}/"),  # 单标签主机
        (NavigationPolicy(), f"http://{long_host}.local/"),  # 本地名空间
        (NavigationPolicy(), f"http://{long_host}..evil/"),  # 主机形态非法
        (NavigationPolicy(mode="allowlist", allowed_domains=["example.com"]), f"http://{long_host}.evil/"),
    ]
    for policy, url in cases:
        ok, reason = policy.validate(url)
        assert not ok, url
        assert len(reason) <= 300, f"reason 无界：{len(reason)} 字符"
        assert "…" in reason, f"缺少截断标记：{reason[-20:]}"
        assert long_host not in reason


def test_protocol_blocked_reason_truncates_long_schemes():
    """同类回显（scheme 也逐字来自 URL 原文）⇒ 同样有界。"""
    ok, reason = NavigationPolicy().validate("a" * 5000 + "://example.com/")
    assert not ok, reason
    assert reason.startswith(REASON_PROTOCOL_BLOCKED), reason[:60]
    assert len(reason) <= 300, f"reason 无界：{len(reason)} 字符"


def test_invalid_url_reasons_truncate_url_derived_exception_text():
    """``urlsplit`` 的 ValueError 消息会把 URL 原文（非法端口/非法 IPv6 括号）抄进来
    ⇒ 异常文本也必须有界（否则同样是无界回显）。"""
    long = "a" * 5000
    for url in (f"http://example.com:{long}/", f"http://[{long}]/"):
        ok, reason = NavigationPolicy().validate(url)
        assert not ok, url
        assert len(reason) <= 300, f"reason 无界：{len(reason)} 字符"
        assert long not in reason


# ── 构造 / 契约 ───────────────────────────────────────────────────────────────


def test_invalid_mode_raises_value_error():
    # 配置写错（拼写错误）必须立刻炸，而不是静默降级成"更宽松的策略"
    with pytest.raises(ValueError, match="allow_https"):
        NavigationPolicy(mode="allow_all")


def test_mode_is_normalized():
    policy = NavigationPolicy(mode=" AllowList ", allowed_domains=["example.com"])
    assert policy.mode == "allowlist"
    assert policy.validate("https://example.com/")[0]


def test_settings_defaults_construct_a_working_policy():
    from browsepilot.app.config import Settings

    settings = Settings(_env_file=None)  # 避开 .env，pin 代码内默认值
    policy = NavigationPolicy(settings.navigation_policy, settings.allowed_domains)
    assert policy.validate("https://example.com/")[0]
    assert not policy.validate("file:///etc/passwd")[0]


def test_no_dns_resolution_only_literal_host_rules_apply():
    """文档化限制：不做 DNS 解析 ⇒ 只按字面主机名判定。

    指向内网的**公有域名**（如 ``db-01.cluster.corp``）在 allow_https 模式下会被放行；
    要挡住它们必须用 allowlist 模式。这是 D4/D7 的既定取舍（无网络调用、纯函数）。
    """
    policy = NavigationPolicy()
    ok, _ = policy.validate("https://db-01.cluster.corp/")
    assert ok, "public-looking hostname is not blocked (no DNS resolution by design)"
