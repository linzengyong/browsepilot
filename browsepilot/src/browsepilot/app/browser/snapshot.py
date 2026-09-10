"""无障碍树快照 → ref 行（纯函数，不 import playwright，可脱离浏览器单测）。

数据来源：Playwright 1.59 的 ``page.aria_snapshot(mode="ai")`` /
``locator.aria_snapshot(mode="ai")``。实测要点：

- ``[ref=eN]`` 标记**只在 ``mode="ai"``** 下产生（默认模式不带 ref）；
- 属性排在 ref 之前：``- generic [active] [ref=e1]:``；
- 可访问名里的引号被转义：``- button "Say \\"hi\\"" [ref=e3]: Go``；
- ref 编号不保证连续（可能只有 e1/e2/e3/e7），故一律以标记为准，不推算序号；
- ref 标记只出现在**条目头**（第一个 ``:`` 之前）：``:`` 之后是正文/文本值，
  正文里出现的 ``[ref=...]`` 是纯文本，不能当条目（否则产出幽灵 ref）；
- 该 ref 可用 Playwright 原生选择器 ``aria-ref=eN`` 定位（动作层 T2 的兜底链第一跳）。

本模块只做字符串处理：``snapshot_to_refs`` 在条目边界截断（绝不切进某个 ref 的块内），
``parse_snapshot_refs`` 给出 :class:`PageSnapshot` 列表，供动作层解析 locator。
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator
from dataclasses import dataclass

__all__ = ["PageSnapshot", "parse_snapshot_refs", "snapshot_to_refs"]

# Playwright ai 模式的 ref 标记，例如 '- heading "Hi" [level=1] [ref=e2]'。
_INLINE_REF_RE = re.compile(r"\[ref=(?P<label>[^\]\s]+)\]")
# YAML 块式形态：ref 是独立键（'- ref: 3' 或 'ref: e7'）。
_BLOCK_REF_RE = re.compile(r"^\s*(?:-\s+)?ref:\s*(?P<label>\S+)\s*$")
# 双引号段（含 \" 转义）：既用于取可访问名，也用于剔除引号内的 [ref=...] 假阳性。
_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
# 行首的 role token：到空白 / 引号 / '[' / ':' 为止。
_ROLE_RE = re.compile(r'^([^\s"\[:]+)')


@dataclass(frozen=True)
class PageSnapshot:
    """一条可被 ref 定位的 AX 树条目（动作层 locator 解析的基础，T2 消费）。"""

    ref_index: int | None = None
    label: str = ""
    role: str = ""
    name: str = ""
    line: str = ""

    @property
    def selector(self) -> str:
        """Playwright 原生 ref 选择器（``aria-ref=eN``）。"""
        return f"aria-ref={self.label}"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _strip_quoted(text: str) -> str:
    """把引号段掏空，避免把可访问名里的 "[ref=e9]" 当成真 ref。"""
    return _QUOTED_RE.sub('""', text)


def _ref_label(line: str) -> str | None:
    """返回该行的 ref token（"e2" / "3"），没有 ref 时返回 None。

    inline 形态只搜条目头（第一个 ``:`` 之前）：Playwright ai 模式总是把 ``[ref=eN]`` 放在
    头部，``:`` 之后是正文/文本值。正文里的 ``[ref=...]``（如 ``- text: use [ref=e12] to click``）
    只是文本：当成条目会占掉一个 ``max_refs`` 名额，且 ``PageSnapshot.selector`` 会传出
    ``aria-ref=e12`` → 动作层点错元素。
    """
    header = _strip_quoted(line).split(":", 1)[0]
    match = _INLINE_REF_RE.search(header)
    if match:
        return match.group("label")
    match = _BLOCK_REF_RE.match(line)
    return match.group("label") if match else None


def _ref_index(label: str) -> int | None:
    match = re.search(r"(\d+)$", label)
    return int(match.group(1)) if match else None


def _iter_ref_blocks(tree: str) -> Iterator[list[str]]:
    """按“从 ref 行到下一个 ref 行之前”切块。

    非 ref 行只有缩进比块首行更深时才算这个块的细节（子文本、块式 YAML 的 role/name 等）；
    与块首同层或更浅的非 ref 行会结束当前块。
    """
    block: list[str] = []
    for line in tree.splitlines():
        if _ref_label(line) is not None:
            if block:
                yield block
            block = [line]
            continue
        if not block or not line.strip():
            continue
        if _indent(line) > _indent(block[0]):
            block.append(line)
        else:
            yield block
            block = []
    if block:
        yield block


def _block_field(block: list[str], key: str) -> str:
    """块式 YAML 里取 ``role:`` / ``name:`` 这类兄弟行字段。"""
    pattern = re.compile(rf"^\s*{key}:\s*(?P<value>.*)$")
    for line in block[1:]:
        match = pattern.match(line)
        if not match:
            continue
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return _unescape(value)
    return ""


def _inline_role_name(line: str) -> tuple[str, str]:
    """ai 模式行内形态：``- <role> "<name>" [attrs] [ref=eN]: <text>``。"""
    body = line.strip()
    if body.startswith("- "):
        body = body[2:].lstrip()
    role_match = _ROLE_RE.match(body)
    role = role_match.group(1) if role_match else ""
    name_match = _QUOTED_RE.search(body)
    return role, _unescape(name_match.group(1)) if name_match else ""


def _block_to_snapshot(block: list[str]) -> PageSnapshot:
    head = block[0]
    label = _ref_label(head) or ""
    if _BLOCK_REF_RE.match(head):
        role, name = _block_field(block, "role"), _block_field(block, "name")
    else:
        role, name = _inline_role_name(head)
    return PageSnapshot(
        ref_index=_ref_index(label),
        label=label,
        role=role,
        name=name,
        line=head,
    )


def parse_snapshot_refs(accessibility_tree: str) -> list[PageSnapshot]:
    """把快照 YAML 解析为 ref 条目列表（顺序＝快照顺序；无 ref 的行不产生条目）。"""
    return [_block_to_snapshot(block) for block in _iter_ref_blocks(accessibility_tree)]


def snapshot_to_refs(accessibility_tree: str, max_refs: int = 30) -> str:
    """把快照 YAML 截断为前 ``max_refs`` 条 ref 行（在条目边界截断，不切进块内）。

    ``max_refs <= 0`` 返回空串；没有 ref 的快照同样返回空串。
    """
    if max_refs <= 0:
        return ""
    blocks = itertools.islice(_iter_ref_blocks(accessibility_tree), max_refs)
    return "\n".join("\n".join(block) for block in blocks)
