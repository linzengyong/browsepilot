"""Check engine v0.3 (P4-D1) — declarative assertions over a ``run_task`` outcome.

设计约束：

- **纯函数、无 I/O**：不看时钟、不读文件、不发网络请求 —— 断言的可信度来自
  "只依赖传进来的 outcome 字典"，所以每一条都能在单测里被钉死。
- **没数据 = FAIL + 明确 detail**（fail-loud）：没有工具调用记录、没有 token 统计、
  没有耗时数据时**绝不按空值通过** —— 宁可产出一条可诊断的 FAIL，也不写一条
  虚假通过的断言（计划尾部的核心原则）。未知 check 类型/mode 同理：返回 FAIL，不抛异常
  （一个笔误不该炸掉整轮评测）。
- **数据源由 eval harness 提供（P4-T3）**：v2 的 ``AgentState`` 目前不携带 token 统计
  （模型层 ``UsageInfo`` 在 answer 节点被丢弃），也不写 ``execution_log``。因此 harness 负责
  把真实模式的用量/耗时/工具名注入 outcome：

  1. token：``token_usage["total_tokens"]`` → ``usage["total_tokens"]`` → 扁平 ``total_tokens``；
     缺失 → FAIL（"no token usage in outcome …"）。
  2. 耗时：优先 ``outcome["duration_ms"]``（整场景墙钟）；否则退化为
     ``execution_log[*]["duration_ms"]`` 之和（语义 = **工具耗时之和**，不含模型调用）；
     两者皆无 → FAIL。
  3. 工具名：以 ``execution_log[*]["tool"]`` 为准（P4-D1）；v2 graph 不写 execution_log，
     所以允许 harness 注入扁平 ``outcome["tool_calls"]``（计划 §Task 1 示例形状），
     仅在 execution_log 一个工具名都取不到时回退。

检查类型（P4-D1）：

============== =======================================================
type           语义
============== =======================================================
field          ``result_data[field] == expected``；``kind: number`` 只断言"是数值"；
               缺 ``field`` / 缺 ``expected`` → FAIL（空声明不得算通过）
contains_keyword  ``final_answer`` 含任一 keyword
not_contains   ``final_answer`` 不含任何 keyword
tools_called   工具名集合：``mode: contains``（默认，全出现）| ``not_contains`` | ``exact_sequence``（有序子序列）
tier_is        ``outcome["tier"] == expected``；缺 ``expected`` → FAIL（空声明不得算通过）
stop_reason_is ``outcome["stop_reason"] == expected``；缺 ``expected`` → FAIL（同上）
tokens_below   token 总量 ≤ ``limit``
latency_below  总耗时 ≤ ``limit_ms``
degradation_absent  ``degradation_log`` 为空
needs_human    ``pending_human_action`` 为真（HITL 场景）
============== =======================================================

``tools_called`` 的期望工具名写 ``tools``（``keywords`` 是同一字段的别名，兼容计划示例）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from eval.scenarios import Check


@dataclass(frozen=True)
class CheckResult:
    """一次断言的结论。``kind`` = 检查类型（``Check.type`` 同值，供报告归类统计）。"""

    passed: bool
    detail: str
    kind: str


def run_check(outcome: dict[str, Any], check: Check) -> CheckResult:
    """对一个 ``run_task`` 的 outcome 求值一条 check。

    未知类型 → ``passed=False`` + ``detail="unknown check type ..."``（fail-loud，不抛异常）。
    """
    handler = _CHECKS.get(check.type)
    if handler is None:
        return CheckResult(False, f"unknown check type {check.type!r}", check.type)
    return handler(outcome, check)


# ── field（既有断言，行为保持不变） ──


def _field(outcome: dict[str, Any], check: Check) -> CheckResult:
    if check.field is None:
        # 空声明（YAML 里写了 `type: field` 但漏了 `field:`）不得按 None == None 通过（I1）
        return CheckResult(
            False,
            "field check 未声明 field（空断言不得算通过；检查 YAML 里的 field 写法）",
            "field",
        )
    value = _result_data(outcome).get(check.field)
    if check.kind == "number":
        return CheckResult(_number(value) is not None, f"{check.field}={value!r} (number)", "field")
    if check.expected is None:
        # 漏写 expected 时，"数据缺失"（None）会与 expected=None 相等 —— 失败运行恰好没有该字段，
        # 这条断言就会在**最不该通过**的运行上通过（I1）。
        return CheckResult(
            False,
            f"field check 未声明 expected（{check.field}={value!r}；空断言不得算通过）",
            "field",
        )
    ok = value == check.expected
    return CheckResult(ok, f"{check.field}={value!r} expected={check.expected!r}", "field")


# ── final_answer 文本断言 ──


def _contains_keyword(outcome: dict[str, Any], check: Check) -> CheckResult:
    keywords = list(check.keywords or [])
    if not keywords:
        return CheckResult(False, "contains_keyword 未声明 keywords（空断言不得算通过）", "contains_keyword")
    text = _text(outcome.get("final_answer"))
    hits = [k for k in keywords if k in text]
    return CheckResult(bool(hits), f"keywords hit={hits}", "contains_keyword")


def _not_contains(outcome: dict[str, Any], check: Check) -> CheckResult:
    keywords = list(check.keywords or [])
    if not keywords:
        return CheckResult(False, "not_contains 未声明 keywords（空断言不得算通过）", "not_contains")
    text = _text(outcome.get("final_answer"))
    hits = [k for k in keywords if k in text]
    return CheckResult(not hits, f"forbidden keywords hit={hits}", "not_contains")


# ── 工具调用断言 ──


def _tools_called(outcome: dict[str, Any], check: Check) -> CheckResult:
    """工具名断言：含（默认）/ 不含 / 顺序（有序子序列）。

    ``exact_sequence`` 按计划语义 = **有序子序列**：期望工具按相对顺序出现即可
    （中间允许夹别的工具）；顺序不对则失败。
    """
    actual = _tool_names(outcome)
    expected = list(check.tools) if check.tools is not None else list(check.keywords or [])
    mode = check.mode or "contains"
    shown = f"tools={actual}" + ("" if actual else " (no tool calls recorded)")
    if not expected:
        return CheckResult(False, f"{shown}, no expected tools declared（空断言不得算通过）", "tools_called")
    if mode == "contains":
        missing = [tool for tool in expected if tool not in actual]
        return CheckResult(not missing, f"{shown} missing={missing}", "tools_called")
    if mode == "not_contains":
        called = [tool for tool in expected if tool in actual]
        return CheckResult(not called, f"{shown} forbidden_called={called}", "tools_called")
    if mode == "exact_sequence":
        ok = _is_subsequence(expected, actual)
        return CheckResult(ok, f"{shown} expected_sequence={expected}", "tools_called")
    return CheckResult(False, f"unknown tools_called mode {mode!r}", "tools_called")


def _tool_names(outcome: dict[str, Any]) -> list[str]:
    """本次运行调用过的工具名（按发生顺序）。

    主源 = ``execution_log[*]["tool"]``（P4-D1，兼容 dict / ``ExecutionStep`` 两种元素）；
    取不到任何工具名时回退到 harness 注入的扁平 ``outcome["tool_calls"]``（P4-T3）——
    v2 graph 目前不写 execution_log，这个回退是计划 §Task 1 示例的形状。
    """
    names: list[str] = []
    for step in _as_list(outcome.get("execution_log")):
        if isinstance(step, dict):
            name = step.get("tool") or step.get("name")
        else:
            name = getattr(step, "tool", None)
        if name:
            names.append(str(name))
    if names:
        return names
    for item in _as_list(outcome.get("tool_calls")):
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            name = item.get("tool") or item.get("name")
            if name:
                names.append(str(name))
        else:
            name = getattr(item, "tool", None) or getattr(item, "name", None)
            if name:
                names.append(str(name))
    return names


def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
    """期望序列是否是实际序列的有序子序列（贪心匹配）。"""
    remaining = iter(actual)
    return all(tool in remaining for tool in expected)


# ── 路由 / 终止断言 ──


def _tier_is(outcome: dict[str, Any], check: Check) -> CheckResult:
    if check.expected is None:
        # 漏写 expected 时，"tier 取不到"（None）会与 expected=None 相等 —— 与 field 同一条
        # 原则：空声明不得算通过，缺失声明时 fail-loud 而不是按"恰好相等"给通过（P4 final review Minor）。
        return CheckResult(
            False,
            "tier_is 未声明 expected（空断言不得算通过；检查 YAML 里的 expected 写法）",
            "tier_is",
        )
    actual = outcome.get("tier")
    return CheckResult(actual == check.expected, f"tier={actual!r} expected={check.expected!r}", "tier_is")


def _stop_reason_is(outcome: dict[str, Any], check: Check) -> CheckResult:
    if check.expected is None:
        # 同 _tier_is：stop_reason 缺失（None）不等于"没写声明"，空声明一律 FAIL（P4 final review Minor）。
        return CheckResult(
            False,
            "stop_reason_is 未声明 expected（空断言不得算通过；检查 YAML 里的 expected 写法）",
            "stop_reason_is",
        )
    actual = outcome.get("stop_reason")
    return CheckResult(
        actual == check.expected,
        f"stop_reason={actual!r} expected={check.expected!r}",
        "stop_reason_is",
    )


# ── 预算断言 ──


def _tokens_below(outcome: dict[str, Any], check: Check) -> CheckResult:
    if check.limit is None:
        return CheckResult(False, "tokens_below 缺少 limit（未声明的预算不得算通过）", "tokens_below")
    total = _total_tokens(outcome)
    if total is None:
        return CheckResult(
            False,
            "no token usage in outcome (token_usage.total_tokens / usage.total_tokens / "
            "total_tokens 都取不到；需要 harness 注入用量，P4-T3) —— 没数据不算通过",
            "tokens_below",
        )
    return CheckResult(total <= check.limit, f"total_tokens={total:g} limit={check.limit}", "tokens_below")


def _total_tokens(outcome: dict[str, Any]) -> float | None:
    """token 总量（``None`` = outcome 里没有任何用量数据）。

    三级取值：``token_usage["total_tokens"]``（计划示例形状）→ ``usage["total_tokens"]``
    （模型层 ``UsageInfo`` 形状）→ 扁平 ``total_tokens``。v2 的 ``AgentState`` 没有 token
    字段（``ChatResult.usage`` 在 answer 节点被丢弃），所以目前只有 harness 显式注入。
    """
    for container in ("token_usage", "usage"):
        box = outcome.get(container)
        if isinstance(box, dict):
            total = _number(box.get("total_tokens"))
            if total is not None:
                return total
    return _number(outcome.get("total_tokens"))


def _latency_below(outcome: dict[str, Any], check: Check) -> CheckResult:
    if check.limit_ms is None:
        return CheckResult(False, "latency_below 缺少 limit_ms（未声明的预算不得算通过）", "latency_below")
    duration = _duration_ms(outcome)
    if duration is None:
        return CheckResult(
            False,
            "no timing data in outcome (duration_ms 缺失，execution_log 里也没有步骤耗时；"
            "需要 harness 注入端到端耗时，P4-T3) —— 没数据不算通过",
            "latency_below",
        )
    return CheckResult(
        duration <= check.limit_ms, f"duration_ms={duration:.1f} limit_ms={check.limit_ms:g}", "latency_below"
    )


def _duration_ms(outcome: dict[str, Any]) -> float | None:
    """总耗时（毫秒，``None`` = 没有任何耗时数据）。

    优先 ``outcome["duration_ms"]``（harness 注入的整场景墙钟）；否则把
    ``execution_log[*]["duration_ms"]`` 求和 —— **语义是工具耗时之和**，不含模型调用，
    门禁阈值要按这个语义设（``outcome["duration_ms"]`` 才是端到端延迟）。
    """
    top = _number(outcome.get("duration_ms"))
    if top is not None:
        return top
    total: float | None = None
    for step in _as_list(outcome.get("execution_log")):
        if isinstance(step, dict):
            value = _number(step.get("duration_ms"))
        else:
            value = _number(getattr(step, "duration_ms", None))
        if value is not None:
            total = (total or 0.0) + value
    return total


# ── 降级 / HITL 断言 ──


def _degradation_absent(outcome: dict[str, Any], check: Check) -> CheckResult:
    entries = _as_list(outcome.get("degradation_log"))
    if not entries:
        return CheckResult(True, "degradation_log empty", "degradation_absent")
    strategies = [str(e.get("strategy", "")) for e in entries if isinstance(e, dict)]
    return CheckResult(
        False, f"degradation_log entries={len(entries)} strategies={strategies}", "degradation_absent"
    )


def _needs_human(outcome: dict[str, Any], check: Check) -> CheckResult:
    pending = bool(outcome.get("pending_human_action"))
    return CheckResult(pending, f"pending_human_action={pending}", "needs_human")


# ── 小工具 ──


def _result_data(outcome: dict[str, Any]) -> dict[str, Any]:
    data = outcome.get("result_data")
    return data if isinstance(data, dict) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _is_number(value: Any) -> bool:
    """数值判定：``bool`` 是 ``int`` 的子类，但不是"数值型数据"（`True` ≠ 温度 1）。"""
    return _number(value) is not None


def _number(value: Any) -> float | None:
    """数值 → ``float``；``bool`` / 字符串 / ``None`` 等非数值 → ``None``（视为"没有数据"）。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


_CHECKS: dict[str, Callable[[dict[str, Any], Check], CheckResult]] = {
    "field": _field,
    "contains_keyword": _contains_keyword,
    "not_contains": _not_contains,
    "tools_called": _tools_called,
    "tier_is": _tier_is,
    "stop_reason_is": _stop_reason_is,
    "tokens_below": _tokens_below,
    "latency_below": _latency_below,
    "degradation_absent": _degradation_absent,
    "needs_human": _needs_human,
}
