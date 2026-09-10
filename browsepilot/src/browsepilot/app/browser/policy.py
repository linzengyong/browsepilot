"""URL 安全策略（D4/D7）：纯函数 + 纯标准库（``urllib.parse`` + ``ipaddress``）。

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
  放行本机（本机调试逃逸口），私有 IP 拦截只在 ``allow_https`` 模式生效。

设计决定（含红线边界）
----------------------
* ``allowlist`` 模式 + ``allowed_domains=None``/空/全空白 ⇒ **全拒**（reason ``allowlist_empty``），
  构造时**不抛错**：配置缺失时策略必须更严而不是更松，也不能让服务起不来。
* 白名单条目只接受裸主机名，构造时归一化（大小写/首尾空白/首尾点号）；**不支持**
  ``*.example.com`` 之类通配符——写错即永不匹配（fail-closed，不会误放行）。
* ``mode`` 未知/拼写错误 ⇒ 构造时 ``ValueError``，绝不由"未知模式"静默降级为更宽松的策略。
* 放行时 reason 为空串；拒绝时 reason 形如 ``code: detail``（``code`` = 下面的 ``REASON_*``），
  可直接用作 T4 面向用户的错误文案。原因串是**稳定契约**，改动需同步 T4。

已知限制
--------
* **不做 DNS 解析**（零网络）：只按字面主机名判定。指向内网的公有域名
  （如 ``db-01.corp.example.com``）在 ``allow_https`` 模式下会放行；要挡住请用 ``allowlist`` 模式。
* 不做端口范围限制（``https://example.com:8443`` 放行），只校验端口可解析。
* 校验当次调用的 URL，不跟踪重定向/子资源请求的后续跳转（T4 每次 navigate 前把关）。
"""

from __future__ import annotations

import ipaddress
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

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


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

    Chromium 会把这些形式解析成 IP 地址，故必须与点分十进制同等对待。
    """
    labels = host.split(".")
    if all(label.isdigit() for label in labels):
        return True
    return any(
        len(label) > 2 and label.startswith("0x") and all(ch in _HEX_DIGITS for ch in label[2:])
        for label in labels
    )


def _local_reason(host: str) -> str | None:
    """主机（已归一化）指向本机/内网时返回拒绝细节，否则 None。"""
    addr = _parse_literal_ip(host)
    if addr is not None:
        if _is_local_ip(addr):
            return f"{host} is a private/loopback/link-local address"
        return None  # 公网字面 IP：不适用主机名启发式
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return f"{host} is a local-only hostname"
    if "." not in host:
        return f"{host} is a single-label host (internal naming)"
    if _looks_like_ipv4_literal(host):
        return f"{host} is an obfuscated IPv4 literal"
    return None


def _normalize_allowlist(entries: list[str] | None) -> tuple[str, ...]:
    """归一化白名单条目（大小写/首尾空白/首尾点号，去重保序），无法使用的条目丢弃。"""
    if not entries:
        return ()
    cleaned: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        domain = entry.strip().lower().strip(".")
        if domain:
            cleaned.append(domain)
    return tuple(dict.fromkeys(cleaned))


def _extract_host(parsed) -> tuple[str, str]:
    """返回 ``(归一化主机, 错误原因)``；无错误时原因为空串。"""
    raw = parsed.hostname  # IPv6 括号非法时抛 ValueError，由调用方按 invalid_url 处理
    if not raw:
        return "", f"{REASON_INVALID_URL}: URL must include a host"
    host = raw.strip().lower().rstrip(".")
    if "%" in host:
        # 主机里的 '%' 只可能是 IPv6 zone id（fe80::1%eth0 / fe80::1%25eth0）；
        # 其余形态（如 %31%32%37.0.0.1 百分号编码）一律非法，防止绕过字面 IP 判定
        base, _, zone = host.partition("%")
        base_ip = _parse_literal_ip(base)
        if not zone or base_ip is None or base_ip.version != 6:
            return "", f"{REASON_INVALID_URL}: percent-encoded host is not allowed"
        host = base
    if not host or host.startswith(".") or ".." in host:
        return "", f"{REASON_INVALID_URL}: malformed host {host!r}"
    return host, ""


def _host_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    """精确匹配或子域后缀匹配（不匹配 lookalike：example.com 不含 notexample.com）。"""
    return any(host == domain or host.endswith("." + domain) for domain in allowed_domains)


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
            parsed = urlsplit(url)
            _port = parsed.port  # 惰性校验：非法端口在此抛 ValueError，必须显式触发
            host, error = _extract_host(parsed)
        except ValueError as exc:
            return False, f"{REASON_INVALID_URL}: {exc}"
        scheme = parsed.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            if not scheme:
                return False, f"{REASON_INVALID_URL}: URL must start with http:// or https://"
            return False, f"{REASON_PROTOCOL_BLOCKED}: {scheme}: is not allowed"
        if error:
            return False, error
        if self.mode == "allowlist":
            if not self.allowed_domains:
                return False, f"{REASON_ALLOWLIST_EMPTY}: no allowed_domains configured, navigation denied"
            if not _host_allowed(host, self.allowed_domains):
                return False, f"{REASON_DOMAIN_NOT_ALLOWED}: {host} is not in the allowlist"
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
            parsed = urlsplit(url)
            host, error = _extract_host(parsed)
        except ValueError:
            return False
        if error or not host:
            return False
        return _local_reason(host) is not None
