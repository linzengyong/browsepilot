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
7. 主机判定前做主机的 WHATWG/UTS46 归一化子集：点变体 ``。``/``．``/``｡`` → ``.``、
   NFKC 兼容字形 → ASCII（全角/上标数字…）、UTS46 ``ignored`` 码点删除。
   ``https://169。254.169.254/`` 在 Chromium 里就是 ``169.254.169.254``（云元数据端点）⇒ 必须拒；
   而 ``https://example。com/`` 归一化后就是 ``example.com`` ⇒ 不能误拒（见下节载荷表）。
8. 白名单里的 IP 字面值/混淆 IPv4 条目（``127.0.0.1``/``1.1.1``/``0x7f.0.0.1``）只做**精确**匹配：
   IP 没有"子域"，``allowed_domains=["1.1.1"]`` 若按后缀匹配会放行回环地址 ``127.1.1.1``。

原因串是 T4 面向用户的错误文案来源，故统一为 ``code: detail`` 形态并逐条 pin 住前缀。
"""

from __future__ import annotations

import pytest

from browsepilot.app.browser.policy import (
    _IGNORED_CODE_POINTS,
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


# ── Unicode 主机归一化：点变体 / 兼容字形 / UTS46 ``ignored`` 码点（⇒ SSRF）────────────
#
# Chromium（WHATWG ``domain to ASCII`` = UTS46，``UseSTD3ASCIIRules=false``）解析主机前先归一化：
#   * ``。``(U+3002)/``．``(U+FF0E)/``｡``(U+FF61) → ``.``；
#   * 兼容字形 → ASCII（全角 ``０ｘ７ｆ`` → ``0x7f``、上标 ``¹²⁷`` → ``127``）；
#   * ``ignored`` 码点（U+00AD 软连字符、U+200B 零宽空格、变体选择符…）直接删除；
#   * U+A7F1 → ``s``（UTS46 ``mapped``，NFKC 不动它）。
# ``urlsplit`` 一个都不做 ⇒ 按原文解析就是"策略放行、浏览器打内网"的解析器差分（SSRF，无需 DNS）。
# 下面每个载荷的真实 host 都用 node v24 的 WHATWG ``URL`` 复核过（完整对照表见 task-3-fix2-report.md）。

UNICODE_PRIVATE_URLS = [
    # (payload, 子拒因关键字)：pin 住归一化后到底由哪条规则拦（私有字面 IP / 混淆 IPv4 / 本机名）
    # U+3002 ``。``
    ("https://169\u3002254.169.254/latest/meta-data/", "private/loopback"),  # 云元数据端点
    ("https://169.254\u3002169.254/", "private/loopback"),  # 点变体在中间：解析器差分最容易被漏掉的形态
    ("https://0x7f\u30020.0.1/", "obfuscated IPv4"),  # 十六进制字面值 + 点变体 ⇒ 127.0.0.1
    ("https://\uff11\uff16\uff19\u3002\uff12\uff15\uff14.169.254/", "private/loopback"),  # 全角数字 + 点变体
    # U+FF0E ``．`` / U+FF61 ``｡``：与 U+3002 同类的另两个点变体
    ("https://169\uff0e254.169.254/", "private/loopback"),
    ("https://169\uff61254.169.254/", "private/loopback"),
    # NFKC 覆盖的"兼容字形 → ASCII"
    ("https://\uff10\uff58\uff17\uff46.0.0.1/", "obfuscated IPv4"),  # 全角 ``0x7f`` ⇒ 混淆字面值
    ("https://\u00b9\u00b2\u2077.0.0.1/", "private/loopback"),  # 上标 ``¹²⁷`` ⇒ 合法字面 IP 127.0.0.1
    # UTS46 ``ignored`` 码点：Chromium 先删除再解析
    ("https://169.25\u00ad4.169.254/", "private/loopback"),  # U+00AD（软连字符）
    ("https://\u00ad169.254.169.254/", "private/loopback"),  # U+00AD 在开头
    ("https://169.254.169.254\u200b/", "private/loopback"),  # U+200B（零宽空格）
    ("https://0\u200bx7f.0.0.1/", "obfuscated IPv4"),  # ignored 码点藏在 ``0x`` 前缀里
    # U+A7F1：UTS46 ``mapped`` → ``s`` ⇒ Chromium host 是 ``foo.localhost``（回环）
    ("https://foo.localho\ua7f1t/", "local-only hostname"),
]


@pytest.mark.parametrize(("url", "subreason"), UNICODE_PRIVATE_URLS)
def test_unicode_host_forms_cannot_hide_private_targets(url, subreason):
    policy = NavigationPolicy()
    ok, reason = policy.validate(url)
    assert not ok, f"{url} must be blocked: Chromium 的 host 归一化会把它变成私有字面值"
    assert reason.startswith(REASON_PRIVATE_ADDRESS_BLOCKED), reason
    # 不能只看 code 前缀（M4）：pin 住是哪条子规则在拦——私有字面 IP / 混淆 IPv4 / 本机名。
    # 走错规则（例如混淆字面值突然被 ipaddress 解析成合法 IP、或合法 IP 掉进混淆分支）会在这里炸
    assert subreason in reason, f"{url} 应由 {subreason!r} 规则拒，实际：{reason}"
    # 独立谓词必须同一套判定（上层复用/展示都读它）
    assert policy.should_block_private(url) is True, url


UNICODE_PUBLIC_URLS = [
    "https://example\u3002com/",
    "https://example\uff0ecom/",
    "https://example\uff61com/",
    "https://www.example\u3002com/",
]


@pytest.mark.parametrize("url", UNICODE_PUBLIC_URLS)
def test_unicode_dot_variants_normalize_to_ascii_dot_without_over_rejecting(url):
    """``example。com`` 的真实主机就是 ``example.com`` ⇒ 既要挡住私有目标，也不能误拒公网主机
    （旧实现把它当单标签主机拒掉，还把这一点写成了"只会多拒（fail-closed）"的承诺——承诺是错的）。"""
    policy = NavigationPolicy()
    assert policy.validate(url) == (True, ""), policy.validate(url)
    assert policy.should_block_private(url) is False, url
    # 归一化后的主机就是 ``example.com``：白名单按 ASCII 主机名条目匹配得上
    allowlist = NavigationPolicy(mode="allowlist", allowed_domains=["example.com"])
    assert allowlist.validate(url) == (True, ""), allowlist.validate(url)


def test_uts46_ignored_code_points_are_removed_from_hosts():
    """``ignored`` 集合的每个区间抽一个代表：Chromium 先删再解析 ⇒ ``169.254.169.2<c>54``
    就是云元数据端点（U+3164/U+FFA0 先 NFKC 成 U+1160，也在 ignored 集合里）。"""
    policy = NavigationPolicy()
    for cp in (
        "\u00ad",  # 软连字符
        "\u034f",
        "\u115f",
        "\u17b4",
        "\u180b",
        "\u200b",  # 零宽空格
        "\u2060",
        "\u206a",
        "\u3164",
        "\ufe00",  # 变体选择符 VS1
        "\ufeff",
        "\uffa0",
        "\U0001bca0",
        "\U0001d173",
        "\U000e0100",  # 变体选择符 VS17
    ):
        url = f"http://169.254.169.2{cp}54/"
        ok, reason = policy.validate(url)
        assert not ok, f"U+{ord(cp):04X} 未从主机中删除：{url!r}"
        assert reason.startswith(REASON_PRIVATE_ADDRESS_BLOCKED), reason


def test_uts46_ignored_code_point_table_invariants():
    """``_IGNORED_CODE_POINTS`` 是安全关键的字面量：Chromium 解析主机前会先删掉这些码点，
    漏收/删错一个区间就是一个误放（``169.254.169.2<c>54`` ⇒ 云元数据端点），而上面的载荷
    测试只抽查代表点——删掉一个区间照样全绿。这里 pin 住整表的不变量：总数（UTS46 17.0.0
    ``status=ignored`` 的全部码点）、无重复（区间重叠等价于漏收码点）与各区间端点/相邻空档。"""
    points = _IGNORED_CODE_POINTS
    assert len(points) == 294, "ignored 码点数变了：改表前先核对 UTS46 IdnaMappingTable.txt"
    assert len(set(points)) == len(points), "表里有重复码点 ⇒ 集合语义下等于漏收码点（误放）"
    for cp in (
        "\u00ad",  # 散列段起点（软连字符）
        "\u034f",
        "\u115f",
        "\u1160",  # NFKC 的落点（\u3164/\uffa0 归一化后就是它）
        "\u17b5",  # 17b4–17b5 区间末尾
        "\u180f",  # 散列段末尾
        "\u200b",  # 零宽空格
        "\u2060",
        "\u206f",  # 206a–206f 区间末尾
        "\u3164",
        "\ufeff",
        "\uffa0",
        "\ufe0f",  # VS1–VS16 区间末尾
        "\U0001bca3",  # 速记格式控制符区间末尾
        "\U0001d17a",  # 音乐符号区间末尾
        "\U000e0100",  # VS17–VS256 区间起点
        "\U000e01ef",  # VS17–VS256 区间末尾
    ):
        assert cp in points, f"U+{ord(cp):04X} 不在 ignored 表里（Chromium 会删除它）"
    for cp in (
        "\u180a",  # 17b4–17b5 之前的 valid 码点
        "\u200c",  # ZWNJ：deviation，Chromium 不删
        "\u200d",
        "\u2065",  # 2060–2064 与 206a–206f 之间的空档
        "\ufe10",  # VS1–VS16 之后
        "\U0001bca4",
        "\U000e00ff",
        "\U000e01f0",  # VS256 之后
    ):
        assert cp not in points, f"U+{ord(cp):04X} 不是 UTS46 ignored 码点，不该被删"


def test_non_ascii_dns_host_is_judged_literally_no_punycode_decoding():
    """文档化限制：不做 punycode（``xn--``）编解码 ⇒ 归一化后仍含非 ASCII 的主机按字面判定。

    ``münchen.de``/``例え.jp`` 在 Chromium 里是 ``xn--`` 开头的 DNS 名（**不是字面 IP**），
    其解析结果是否指向内网属于既有的"不做 DNS 解析"限制。反向保证由上面两个载荷表兜住：
    凡是 Chromium 会解析成字面 IP/本机名的形态，本策略都拒。
    """
    policy = NavigationPolicy()
    for url in ("https://m\u00fcnchen.de/", "https://\u4f8b\u3048.jp/"):
        assert policy.validate(url) == (True, ""), policy.validate(url)
        assert policy.should_block_private(url) is False, url


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


def test_allowlist_entries_use_the_same_unicode_host_mapping():
    """条目与主机共用同一套归一化：``Example。COM.`` ≡ ``example.com``；
    全角数字条目 ``１２７.0.0.1`` ≡ ``127.0.0.1``（IP 字面值条目 ⇒ 只精确匹配，见下）。"""
    policy = NavigationPolicy(
        mode="allowlist", allowed_domains=["Example\u3002COM.", "\uff11\uff12\uff17.0.0.1"]
    )
    for url in ("https://example.com/", "https://example\u3002com/", "http://127.0.0.1:8099/x"):
        assert policy.validate(url) == (True, ""), policy.validate(url)
    for url in ("https://evil.com/", "http://127.0.0.2/"):
        ok, reason = policy.validate(url)
        assert not ok, url
        assert reason.startswith(REASON_DOMAIN_NOT_ALLOWED), reason


@pytest.mark.parametrize(
    ("entry", "suffix_host"),
    [
        ("1.1.1", "127.1.1.1"),  # 回环：``host.endswith(".1.1.1")`` 会把 127.1.1.1 放进来
        ("1.1.1", "1.1.1.1"),  # 同一个条目的"看似子域"形式
        ("0x7f.0.0.1", "evil.0x7f.0.0.1"),  # ipaddress 解析不了、但形状就是混淆 IPv4
    ],
)
def test_allowlist_ip_literal_entries_require_exact_match(entry, suffix_host):
    """IP 字面值/混淆 IPv4 条目不做子域后缀匹配——IP 没有"子域"：
    ``allowed_domains=["1.1.1"]`` 按后缀匹配会放行回环地址 ``127.1.1.1``。"""
    policy = NavigationPolicy(mode="allowlist", allowed_domains=[entry])
    ok, reason = policy.validate(f"http://{suffix_host}/")
    assert not ok, f"{suffix_host} must not be authorized by the IP-literal entry {entry!r}"
    assert reason.startswith(REASON_DOMAIN_NOT_ALLOWED), reason


def test_allowlist_exact_ip_literal_entry_still_allows_that_host():
    """逃逸口不受收紧影响：显式写 ``127.0.0.1``/``::1`` 仍精确放行该主机（本地调试）。"""
    policy = NavigationPolicy(mode="allowlist", allowed_domains=["127.0.0.1", "::1"])
    for url in ("http://127.0.0.1:8099/x", "http://[::1]:8099/x"):
        assert policy.validate(url) == (True, ""), policy.validate(url)


def test_obfuscated_ipv4_literal_detection_is_keyed_on_the_last_label():
    """WHATWG 只在主机**以数字结尾**时才尝试 IPv4 解析（最后一个 label 是十进制/``0x`` 数字），
    不是"任意 label 长得像 ``0x…``"：``0x7f.example.com`` 的最后一段是 ``com``，Chromium
    把它当 DNS 名 ⇒ ``allow_https`` 不能误拒（``0x7f.com``/``0x7f.0.0.1.example.com`` 同理）。"""
    policy = NavigationPolicy()
    for url in (
        "https://0x7f.example.com/",
        "https://0x7f.com/",
        "https://0x7f.0.0.1.example.com/",
    ):
        assert policy.validate(url) == (True, ""), policy.validate(url)


def test_allowlist_entry_whose_first_label_looks_like_0x_is_still_a_dns_suffix():
    """M3：条目 ``0x7f.com`` 不以数字结尾 ⇒ Chromium 走 DNS 名（不是字面 IP）⇒ 不该被
    当成"IP 字面值条目"降级为只精确匹配：子域 ``www.0x7f.com`` 仍由后缀匹配放行。"""
    policy = NavigationPolicy(mode="allowlist", allowed_domains=["0x7f.com"])
    for url in ("https://0x7f.com/x", "https://www.0x7f.com/x"):
        assert policy.validate(url) == (True, ""), policy.validate(url)
    ok, reason = policy.validate("https://evil.com/")
    assert not ok, "unrelated host must stay blocked"
    assert reason.startswith(REASON_DOMAIN_NOT_ALLOWED), reason


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


def test_rejection_reasons_stay_bounded_for_non_ascii_hosts():
    """截断必须发生在**拼好整条 detail 之后**：先 ``_echo(host)`` 再 ``!r`` 的话，
    repr 会把 200 个不可打印的非 ASCII 字符转义回 6 倍长（``U+200E`` → ``\\u200e``），
    300 上限形同虚设（5000 字符主机 ⇒ 拒因 1200+ 字符）。"""
    # U+200E（Cf，非 ASCII 且不可打印）+ ".." ⇒ 走 malformed host 分支
    host = "\u200e" * 5000 + "..evil"
    ok, reason = NavigationPolicy().validate(f"http://{host}/")
    assert not ok, "malformed host must be rejected"
    assert reason.startswith(REASON_INVALID_URL), reason
    assert len(reason) <= 300, f"reason 无界：{len(reason)} 字符"
    assert "\u2026" in reason, f"缺少截断标记：{reason[-20:]}"
    # M4：把有界性写成可证伪的具体断言，而不是恒真的长度不等式。截断作用于**拼好的整条
    # detail** ⇒ detail = _echo 的 200 字符上限 + 省略号；若先 _echo(host) 再 !r，200 个
    # U+200E 会各自膨胀成 6 字符（200 个 ``\u200e``），下面两条都会炸——旧的
    # ``len(reason) - count * 5 < 300`` 在那种情况下仍然恒真，等于没测。
    detail = reason.removeprefix(f"{REASON_INVALID_URL}: ")
    assert detail.startswith("malformed host "), detail[:40]
    assert len(detail) <= 201, f"detail 未截断到 200+省略号：{len(detail)} 字符"
    escapes = detail.count("\\u200e")
    assert escapes <= 33, f"转义序列未被截断：{escapes}"


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
