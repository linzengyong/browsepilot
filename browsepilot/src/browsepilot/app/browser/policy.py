r"""URL 安全策略（D4/D7）：纯函数 + 纯标准库（``urllib.parse`` + ``ipaddress`` + ``unicodedata``）。

层次红线
--------
本模块属于 app 层，只依赖标准库，**不** import ``browsepilot.agent.*``（由
``tests/test_architecture.py`` 守卫）；不需要浏览器、不发起任何网络请求，可离线单测。

接口契约
--------
``NavigationPolicy(mode="allow_https", allowed_domains=None)``
``validate(url) -> (ok, reason)`` / ``should_block_private(url) -> bool``

* ``mode="allow_https"``（默认，对应 ``Settings.navigation_policy``）：只放行 ``http``/``https``；
  其余 scheme（``file:``/``javascript:``/``data:``/``ftp:``/``about:``/``chrome:``/``blob:`` …）
  一律拒；字面私有/回环/链路本地/保留地址（IPv4+IPv6）、本地主机名
  （``localhost``、``*.local``/``*.internal``/``*.home.arpa``、单标签主机名）以及
  整数形式的 IPv4 字面值（``2130706433``、``0177.0.0.1``、``0x7f.0.0.1``——Chromium 会
  解析成 IP）一律拒。URL 自身的 scheme/主机名不能绕过判定。
* ``mode="allowlist"``：在 scheme 门禁之上，只放行 ``allowed_domains`` 中的主机——精确匹配或
  子域后缀匹配（``example.com`` 放行 ``www.example.com``，但**不**放行 ``notexample.com``）。
  该模式的信任边界就是"运维显式写下的域名"：显式写 ``localhost``/``127.0.0.1`` 即视为有意
  放行本机（本机调试逃逸口），私有 IP 拦截只在 ``allow_https`` 模式生效。IP 字面值/混淆 IPv4
  条目（``127.0.0.1``/``1.1.1``/``0x7f.0.0.1``）**只做精确匹配**：IP 没有"子域"，
  ``allowed_domains=["1.1.1"]`` 若按后缀匹配会放行回环地址 ``127.1.1.1``。
* 主机按 **WHATWG/Chromium 语义**判定，判定前做两个归一化（**交给导航的原始 URL 不动**）：
  1. ``\`` ↔ ``/``（特殊 scheme 的 authority 终止符）：``urlsplit`` 不做 ⇒ 按原文解析会把
     ``https://127.0.0.1\@example.com/`` 读成 host ``example.com``（allowlist 放行），
     Chromium 却访问 ``127.0.0.1``（SSRF）；
  2. 主机级 **UTS46 归一化子集**（``_whatwg_normalized_host``）：点变体
     ``。``/``．``/``｡`` → ``.``、NFKC 兼容字形 → ASCII（全角/上标数字…）、UTS46 ``ignored``
     码点删除（外加 ``\ua7f1`` → ``s``）。Chromium 解析主机前会做同一套映射，``urlsplit`` 不做 ⇒
     ``https://169。254.169.254/`` 被读成普通域名（放行），Chromium 却访问云元数据端点
     ``169.254.169.254``——同一类**解析器差分**（两边不会不一致的情况见"已知限制"）。

设计决定（含红线边界）
----------------------
* ``allowlist`` 模式 + ``allowed_domains=None``/空/全空白 ⇒ **全拒**（reason ``allowlist_empty``），
  构造时**不抛错**：配置缺失时策略必须更严而不是更松，也不能让服务起不来。
* 白名单条目只接受裸主机名，构造时归一化（大小写/首尾空白/首尾点号）；**不支持**
  ``*.example.com`` 之类通配符——写错即永不匹配（fail-closed，不会误放行）。
* **无点号的裸标签条目、IP 字面值/混淆 IPv4 条目都只做精确匹配**：对裸标签做子域后缀匹配等于
  授权整个 TLD——``allowed_domains=["com"]`` 会放行 ``evil.com``；而 IP 没有"子域"——
  ``allowed_domains=["1.1.1"]`` 若按后缀匹配会放行回环地址 ``127.1.1.1``。
* 拒因里所有来自 URL 的回显（主机/scheme/异常消息）截断到 ``_MAX_ECHO_CHARS`` 字符 + 省略号：
  拒因会进入 ``ToolResult``/agent 状态，必须与其他状态数据一样有界（同 actions.py 的
  ``_MAX_ERROR_CHARS`` 约定）；只截断 detail 的长回显，``code`` 前缀与结构不变。**先拼好整条
  detail 再截断**：先 ``_echo(host)`` 再 ``!r``/格式化，会让不可打印字符在 repr 里重新膨胀
  （``U+200E`` → 6 字符）——截断等于没截。
* ``mode`` 未知/拼写错误 ⇒ 构造时 ``ValueError``，绝不由"未知模式"静默降级为更宽松的策略。
* 放行时 reason 为空串；拒绝时 reason 形如 ``code: detail``（``code`` = 下面的 ``REASON_*``），
  可直接用作 T4 面向用户的错误文案。原因串是**稳定契约**，改动需同步 T4。

已知限制
--------
* **不做 DNS 解析**（零网络）：只按字面主机名判定。指向内网的公有域名
  （如 ``db-01.corp.example.com``）在 ``allow_https`` 模式下会放行；要挡住请用 ``allowlist`` 模式。
* 不做端口范围限制（``https://example.com:8443`` 放行），只校验端口可解析。
* **不做完整 IDNA/UTS46**：只做上面第 2 条的归一化子集，不做 punycode（``xn--``）编解码，也不对齐
  IDNA2003↔UTS46 的其余差异（``ß``/词尾 ``ς``/Cherokee 小写…）。归一化后仍含非 ASCII 的主机按字面
  判定，分两类：UTS46 ``valid``/``mapped`` 的码点在 Chromium 里是 ``xn--`` 开头的 **DNS 名**（不是
  字面 IP/本机名），是否指向内网属于上面"不做 DNS 解析"的取舍；UTS46 ``disallowed`` 的码点
  （U+200E/200F/061C、U+2024/2025、U+FE52、C1 控制符…）会让 Chromium 的 ``domain to ASCII`` 直接
  失败、**拒绝整个 URL**，本层按字面判定则可能放行——方向安全（放行的是浏览器根本不会导航的
  URL），不构成"策略放行、浏览器打内网"的误放。归一化用 NFKC + 显式映射：NFKC 的映射面比
  UTS46 ``mapped`` 更宽——UTS46 标 ``valid``/``disallowed`` 的兼容字形它也会先映射（不少直接
  落成 ASCII，如 ``U+FE52``→``.``）⇒ 有些在 Chromium 里是 DNS 名（或会被拒）的主机在本层会被
  当成字面 IP/本机名判——偏离方向是更严，不是漏放。反向保证（本层的契约）：**凡 Chromium 会解析成字面 IP/本机名的形态（点变体、
  全角/兼容数字、``ignored`` 码点、``\ua7f1``）也都会按 IP/本机名拒**——这一条逐条核对过 UTS46
  17.0.0 ``IdnaMappingTable.txt``：``mapped``/``disallowed_STD3_mapped`` 共 6377 个码点，映射结果
  含 ASCII 的 1673 个里 NFKC 复现 1669 个，其余三个与判定有关的（``\u3002``/``\uff61``/``\ua7f1``）
  由本层显式映射；``ignored`` 码点已全量列入 ``_IGNORED_CODE_POINTS``。
* 校验当次调用的 URL，不跟踪重定向/子资源请求的后续跳转（T4 每次 navigate 前把关）。
"""

from __future__ import annotations

import ipaddress
import unicodedata
from urllib.parse import urlsplit

__all__ = [
    "MODES",
    "REASON_ALLOWLIST_EMPTY",
    "REASON_DOMAIN_NOT_ALLOWED",
    "REASON_INVALID_URL",
    "REASON_PRIVATE_ADDRESS_BLOCKED",
    "REASON_PROTOCOL_BLOCKED",
    "NavigationPolicy",
]

MODES = ("allow_https", "allowlist")

REASON_INVALID_URL = "invalid_url"
REASON_PROTOCOL_BLOCKED = "protocol_blocked"
REASON_PRIVATE_ADDRESS_BLOCKED = "private_address_blocked"
REASON_DOMAIN_NOT_ALLOWED = "domain_not_allowed"
REASON_ALLOWLIST_EMPTY = "allowlist_empty"

_ALLOWED_SCHEMES = ("http", "https")
# 标准化的"本机/内网"名空间：RFC 6761 localhost、RFC 6762 mDNS .local、ICANN .internal、RFC 8375 home.arpa
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_CONTROL_CHARS = "\t\r\n\v\f"
_HEX_DIGITS = frozenset("0123456789abcdef")

# ── WHATWG/UTS46 主机归一化：只收录**能把主机变成字面 IP / 本机名**的映射 ──────────────
# （用不到的映射不收：归一化得越多，越容易和 Chromium 的真正实现产生新的差分）
_DOT_VARIANTS = ("\u3002", "\uff0e", "\uff61")  # 。．｡：UTS46 里全部映射到 "." 的码点
_ASCII_MAPPINGS = {**dict.fromkeys(_DOT_VARIANTS, "."), "\ua7f1": "s"}
"""本层显式映射的 UTS46 ASCII 条目（值 = Chromium/UTS46 的映射结果）。

* 三个点变体 → ``.``：UTS46 里**全部**映射到 ``.`` 的码点恰好就是这三个（``\uff0e`` 虽在 NFKC
  的覆盖范围内，但同属一条规则，一并显式列出）；
* ``\ua7f1`` → ``s``：逐条核对 UTS46 17.0.0 ``IdnaMappingTable.txt``（WHATWG ``domain to ASCII``
  传 ``UseSTD3ASCIIRules=false`` ⇒ ``disallowed_STD3_mapped`` 也参与映射）——6377 个 ``mapped``
  码点里 1673 个的映射结果含 ASCII 字符，1669 个由 NFKC 复现；复现不了的 4 个里只有这三个与
  "字面 IP/本机名"判定有关（另一个 U+037A 是"ASCII 空格 + 非 ASCII 连字"）。不映射 ``\ua7f1``
  的话，``foo.localho\ua7f1t`` 在 Chromium 里是 ``foo.localhost`` ⇒ 回环地址。
"""
_IGNORED_CODE_POINTS = (
    "\u00ad\u034f\u115f\u1160\u17b4\u17b5\u180b\u180c\u180d\u180e\u180f\u200b"
    "\u2060\u2061\u2062\u2063\u2064\u206a\u206b\u206c\u206d\u206e\u206f\u3164\ufeff\uffa0"
    + "".join(map(chr, range(0xFE00, 0xFE10)))  # 变体选择符 VS1–VS16
    + "".join(map(chr, range(0x1BCA0, 0x1BCA4)))  # 速记格式控制符
    + "".join(map(chr, range(0x1D173, 0x1D17B)))  # 音乐符号起始/终止梁
    + "".join(map(chr, range(0xE0100, 0xE01F0)))  # 变体选择符 VS17–VS256
)
"""UTS46 ``status=ignored`` 的全部 294 个码点：Chromium 解析主机时**直接删除**。

``169.25\u00ad4.169.254``、``169.254.169.254\u200b`` 在 Chromium 里都是 ``169.254.169.254``
（云元数据端点）——少删一个码点就是一个误放，故按表逐区间列全。
"""
_URL_TRANSLATION = {ord("\\"): "/", **{ord(ch): "." for ch in _DOT_VARIANTS}}
_HOST_TRANSLATION = {
    **{ord(ch): None for ch in _IGNORED_CODE_POINTS},
    **{ord(ch): mapped for ch, mapped in _ASCII_MAPPINGS.items()},
}
_MAX_ECHO_CHARS = 200
"""拒因里回显的 URL 片段（主机/scheme/异常消息）上限（CLAUDE.md 教训 §4：进状态的数据
一律截断；同 actions.py 的 ``_MAX_ERROR_CHARS``）。"""

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _echo(value: str) -> str:
    """回显用片段：超长截断 + 省略号（拒因长度必须有界，且保持 ``code: detail`` 形态）。"""
    return value if len(value) <= _MAX_ECHO_CHARS else value[:_MAX_ECHO_CHARS] + "…"


def _whatwg_normalized_url(url: str) -> str:
    r"""把 URL 里两处 WHATWG 等价形式归一化，**只用于判定主机**。

    * ``\`` ↔ ``/``：特殊 scheme（http/https）下两者都是 authority 终止符。``urlsplit`` 不把
      ``\`` 当终止符：``https://127.0.0.1\@example.com/`` 被读成 host ``example.com``
      （allowlist 放行），Chromium 却访问 ``127.0.0.1`` ⇒ SSRF；
    * 点变体 ``\u3002``/``\uff0e``/``\uff61`` ↔ ``.``：Chromium 判定主机前把它们映射成 ``.``
      （``https://169。254.169.254/`` 的真实主机是 ``169.254.169.254``，云元数据端点），
      ``urlsplit`` 不做 ⇒ 同一类解析器差分（SSRF，无需 DNS）。

    **不改动**交给导航的原始 URL（T4 仍用原文 navigate）。
    """
    return url.translate(_URL_TRANSLATION)


def _whatwg_normalized_host(host: str) -> str:
    r"""把主机映射成 Chromium 判定时会用的形态：UTS46 归一化的**安全子集**（纯标准库、零网络）。

    顺序：NFKC → 删 UTS46 ``ignored`` 码点 → 显式 ASCII 映射（点变体、``\ua7f1``）→ 小写。

    * NFKC 覆盖"兼容字形 → ASCII"：``０ｘ７ｆ.0.0.1`` → ``0x7f.0.0.1``（127.0.0.1）、
      ``¹²⁷.0.0.1`` → ``127.0.0.1``、``．`` → ``.``。NFKC 在删除**之前**跑：``\u3164``/``\uffa0``
      的 NFKC 结果是 U+1160，也属于 ``ignored``，才会被一并删掉；
    * ``ignored`` 码点（U+00AD/U+200B/变体选择符…，见 ``_IGNORED_CODE_POINTS``）：Chromium 先删再解析；
    * ``\u3002``/``\uff61`` → ``.``、``\ua7f1`` → ``s``：NFKC 不动的 UTS46 ASCII 映射（见常量注释）；
    * 小写放在最后：NFKC 会把全角大写字母变成大写 ASCII。

    **不做** punycode（``xn--``）编解码、也不对齐 IDNA2003↔UTS46 的其余差异：归一化后仍含非 ASCII
    的主机按字面判定——UTS46 ``valid``/``mapped`` 的码点在 Chromium 里是 ``xn--`` DNS 名（不是字面
    IP/本机名），而 ``disallowed`` 的码点（U+200E/200F/061C、U+2024/2025、U+FE52、C1 控制符…）会让
    Chromium 直接拒绝整个 URL、这里却可能放行（方向安全，详见模块 docstring"已知限制"）；反之，
    凡 Chromium 会解析成字面 IP/本机名的形态，这里都会归一化成同样的 ASCII 值。另外，NFKC 的
    映射面比 UTS46 ``mapped`` 更宽（UTS46 标 ``valid``/``disallowed`` 的兼容字形它也会先映射）
    ⇒ 有些主机在本层被当成字面 IP/本机名判严——偏离方向是更严，不是漏放。
    """
    return unicodedata.normalize("NFKC", host).translate(_HOST_TRANSLATION).lower()


def _parse_literal_ip(host: str) -> _IPAddress | None:
    """主机字面值是 IP 时返回地址对象，否则 None（不做 DNS）。"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_local_ip(addr: _IPAddress) -> bool:
    """非公网可达 / 私有 / 回环 / 链路本地 / 保留 / 未指定 / 组播。"""
    return (
        not addr.is_global
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
    )


def _looks_like_ipv4_literal(host: str) -> bool:
    """整数/八进制/十六进制形式的 IPv4 字面值（如 2130706433、0177.0.0.1、0x7f.0.0.1）。

    Chromium 会把这些形式解析成 IP 地址，故必须与点分十进制同等对待。**触发条件与 WHATWG 对齐**：
    只在主机**以数字结尾**（最后一个 label 是十进制/``0x`` 十六进制数字）时才尝试 IPv4 解析；
    最后一个 label 不是数字时 Chromium 走 DNS 名（``0x7f.example.com``/``0x7f.com`` 即使第一个
    label 长得像十六进制字面值也一样），不能误拒，对应的 allowlist 条目也不能降级成"只精确匹配"。
    """
    last = host.rsplit(".", 1)[-1]
    if last.isdigit():
        return True
    return len(last) > 2 and last.startswith("0x") and all(ch in _HEX_DIGITS for ch in last[2:])


def _local_reason(host: str) -> str | None:
    """主机（已归一化）指向本机/内网时返回拒绝细节，否则 None。"""
    addr = _parse_literal_ip(host)
    if addr is not None:
        if _is_local_ip(addr):
            return f"{_echo(host)} is a private/loopback/link-local address"
        return None  # 公网字面 IP：不适用主机名启发式
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return f"{_echo(host)} is a local-only hostname"
    if "." not in host:
        return f"{_echo(host)} is a single-label host (internal naming)"
    if _looks_like_ipv4_literal(host):
        return f"{_echo(host)} is an obfuscated IPv4 literal"
    return None


def _normalize_allowlist(entries: list[str] | None) -> tuple[str, ...]:
    """归一化白名单条目（与主机同一套归一化 + 首尾空白/首尾点号，去重保序），无法使用的条目丢弃。

    条目与主机共用 ``_whatwg_normalized_host``：运维写 ``Example。COM.`` 就等于 ``example.com``
    （否则"条目写对了但永不匹配"会变成静默 fail-closed）。
    """
    if not entries:
        return ()
    cleaned: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        domain = _whatwg_normalized_host(entry.strip()).strip(".")
        if domain:
            cleaned.append(domain)
    return tuple(dict.fromkeys(cleaned))


def _extract_host(parsed) -> tuple[str, str]:
    """返回 ``(归一化主机, 错误原因)``；无错误时原因为空串。"""
    raw = parsed.hostname  # IPv6 括号非法时抛 ValueError，由调用方按 invalid_url 处理
    if not raw:
        return "", f"{REASON_INVALID_URL}: URL must include a host"
    host = _whatwg_normalized_host(raw.strip())
    if "%" in host:
        # 主机里的 '%' 只可能是 IPv6 zone id（fe80::1%eth0 / fe80::1%25eth0）；
        # 其余形态（如 %31%32%37.0.0.1 百分号编码）一律非法，防止绕过字面 IP 判定
        # （归一化在前，全角 ％ 之类的变体也跑不掉）
        base, _, zone = host.partition("%")
        base_ip = _parse_literal_ip(base)
        if not zone or base_ip is None or base_ip.version != 6:
            return "", f"{REASON_INVALID_URL}: percent-encoded host is not allowed"
        host = base
    host = host.rstrip(".")
    if not host or host.startswith(".") or ".." in host:
        # **先拼好整条 detail 再截断**（M2b）：先 _echo(host) 再 !r 会让不可打印字符
        # 在 repr 里重新膨胀（U+200E → \\u200e，6 字符），截断等于没截
        detail = _echo(f"malformed host {host!r}")
        return "", f"{REASON_INVALID_URL}: {detail}"
    return host, ""


def _is_ip_literal_entry(entry: str) -> bool:
    """条目是否"长成 IP 字面值"（点分十进制，或整数/八进制/十六进制混淆形态）。"""
    return _parse_literal_ip(entry) is not None or _looks_like_ipv4_literal(entry)


def _host_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    """精确匹配或子域后缀匹配（不匹配 lookalike：example.com 不含 notexample.com）。

    两类条目只做**精确**匹配——"后缀/子域"对它们没有意义，做了就是放大授权：

    * 无点号的裸标签条目（``localhost``/``::1``）：对裸标签做后缀匹配等于授权整个 TLD——
      ``allowed_domains=["com"]`` 会放行 ``evil.com``，白名单形同虚设；
    * IP 字面值/混淆 IPv4 条目（``127.0.0.1``/``1.1.1``/``0x7f.0.0.1``）：IP 没有"子域"，
      ``allowed_domains=["1.1.1"]`` 按后缀匹配会放行回环地址 ``127.1.1.1``。
    """
    return any(
        host == domain
        or ("." in domain and not _is_ip_literal_entry(domain) and host.endswith("." + domain))
        for domain in allowed_domains
    )


class NavigationPolicy:
    """导航前判定的 URL 安全策略（无状态、纯函数式、线程安全）。"""

    def __init__(self, mode: str = "allow_https", allowed_domains: list[str] | None = None) -> None:
        if not isinstance(mode, str) or mode.strip().lower() not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        self.mode: str = mode.strip().lower()
        #: 归一化后的白名单（元组：不可变，避免调用方事后改写策略行为）；allow_https 模式下不参与判定
        self.allowed_domains: tuple[str, ...] = _normalize_allowlist(allowed_domains)

    def validate(self, url: str) -> tuple[bool, str]:
        """放行返回 ``(True, "")``；拒绝返回 ``(False, "code: detail")``。"""
        if not isinstance(url, str):
            return False, f"{REASON_INVALID_URL}: URL must be a string"
        if not url or url.strip() != url:
            return False, f"{REASON_INVALID_URL}: URL must be non-empty without surrounding whitespace"
        if any(ch in _CONTROL_CHARS or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
            return False, f"{REASON_INVALID_URL}: URL must not contain control characters"
        try:
            parsed = urlsplit(_whatwg_normalized_url(url))
            _port = parsed.port  # 惰性校验：非法端口在此抛 ValueError，必须显式触发
            host, error = _extract_host(parsed)
        except ValueError as exc:
            # 异常消息里可能回显 URL 原文（端口/IPv6 括号）⇒ 同样有界
            return False, f"{REASON_INVALID_URL}: {_echo(str(exc))}"
        scheme = parsed.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            if not scheme:
                return False, f"{REASON_INVALID_URL}: URL must start with http:// or https://"
            return False, f"{REASON_PROTOCOL_BLOCKED}: {_echo(scheme)}: is not allowed"
        if error:
            return False, error
        if self.mode == "allowlist":
            if not self.allowed_domains:
                return False, f"{REASON_ALLOWLIST_EMPTY}: no allowed_domains configured, navigation denied"
            if not _host_allowed(host, self.allowed_domains):
                return False, f"{REASON_DOMAIN_NOT_ALLOWED}: {_echo(host)} is not in the allowlist"
            return True, ""
        reason = _local_reason(host)
        if reason:
            return False, f"{REASON_PRIVATE_ADDRESS_BLOCKED}: {reason}"
        return True, ""

    def should_block_private(self, url: str) -> bool:
        """主机字面值是否为本机/私有地址（模式无关的独立查询）。

        无法解析/无主机的 URL 返回 ``False``——那是"非法"而非"私有"，合法性由 :meth:`validate` 负责。
        """
        if not isinstance(url, str) or not url:
            return False
        try:
            parsed = urlsplit(_whatwg_normalized_url(url))
            host, error = _extract_host(parsed)
        except ValueError:
            return False
        if error or not host:
            return False
        return _local_reason(host) is not None
