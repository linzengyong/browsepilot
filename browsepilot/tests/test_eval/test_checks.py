"""Tests for the v0.3 check engine (P4-D1): ``eval/checks.py``.

每一类断言都要有**正例 + 反例 + 边界**（数据缺失 / 空声明 / 恰好等于预算）。
核心原则：没有数据不得算通过 —— 宁可 FAIL + 明确 detail，也不写虚假通过的断言。
"""

from __future__ import annotations

from eval.checks import CheckResult, run_check
from eval.scenarios import Check


def _outcome(**overrides) -> dict:
    """一个"跑完但什么内容都没有"的 outcome：各测试只声明自己关心的字段。"""
    base: dict = {
        "tier": "tier1",
        "stop_reason": "completed",
        "final_answer": "",
        "result_data": {},
        "execution_log": [],
        "degradation_log": [],
        "pending_human_action": False,
    }
    base.update(overrides)
    return base


# ── field（既有断言：result_data[field] == expected / kind=number） ──


def test_field_string_match_and_mismatch():
    outcome = _outcome(result_data={"city": "北京"})
    assert run_check(outcome, Check(type="field", field="city", expected="北京")).passed
    assert not run_check(outcome, Check(type="field", field="city", expected="上海")).passed


def test_field_number_kind_accepts_int_and_float():
    for value in (26, 26.0):
        result = run_check(
            _outcome(result_data={"temperature_c": value}),
            Check(type="field", field="temperature_c", kind="number"),
        )
        assert result.passed, value


def test_field_number_kind_rejects_bool_and_numeric_string():
    # bool 是 int 的子类 —— True 不是"数值型温度"；"26"（字符串）同样不是数值
    assert not run_check(
        _outcome(result_data={"temperature_c": True}),
        Check(type="field", field="temperature_c", kind="number"),
    ).passed
    assert not run_check(
        _outcome(result_data={"temperature_c": "26"}),
        Check(type="field", field="temperature_c", kind="number"),
    ).passed


def test_field_missing_key_fails():
    assert not run_check(
        _outcome(), Check(type="field", field="city", expected="北京")
    ).passed


def test_field_empty_declaration_fails_loud():
    """I1：空声明（漏 ``field`` / 漏 ``expected``）不得算通过。

    失败运行的 ``result_data`` 是空的：漏写 ``expected`` 时 ``None == None`` 恰好成立 ——
    这条断言会在**最不该通过**的运行上通过。
    """
    only_type = run_check(_outcome(), Check(type="field"))
    assert not only_type.passed
    assert "field" in only_type.detail

    missing_expected = run_check(_outcome(), Check(type="field", field="city"))
    assert not missing_expected.passed
    assert "expected" in missing_expected.detail

    # 数据本身就缺（None）也不例外："没写 expected"与"数据里真的是 None"不是一回事
    assert not run_check(
        _outcome(result_data={"city": None}), Check(type="field", field="city")
    ).passed

    # kind=number 只断言"是数值"，不要求 expected（但 field 仍必须有）
    assert run_check(
        _outcome(result_data={"temperature_c": 26.0}),
        Check(type="field", field="temperature_c", kind="number"),
    ).passed


def test_field_result_carries_kind_and_detail():
    result = run_check(
        _outcome(result_data={"city": "北京"}),
        Check(type="field", field="city", expected="北京"),
    )
    assert isinstance(result, CheckResult)
    assert result.kind == "field"
    assert "city" in result.detail


# ── contains_keyword / not_contains ──


def test_contains_keyword_positive_and_negative():
    outcome = _outcome(final_answer="Python 3.13 于 2024 年发布")
    assert run_check(outcome, Check(type="contains_keyword", keywords=["2024"])).passed
    assert not run_check(outcome, Check(type="contains_keyword", keywords=["2025"])).passed


def test_contains_keyword_boundary_any_hit_empty_declaration_and_empty_answer():
    outcome = _outcome(final_answer="发布于 2024 年")
    # 任一命中即可
    assert run_check(
        outcome, Check(type="contains_keyword", keywords=["2025", "2024"])
    ).passed
    # 空声明 = 什么也没断言 → 不得算通过
    assert not run_check(outcome, Check(type="contains_keyword")).passed
    # 空回答
    assert not run_check(
        _outcome(final_answer=""), Check(type="contains_keyword", keywords=["x"])
    ).passed


def test_not_contains_positive_and_negative():
    assert run_check(
        _outcome(final_answer="抱歉，这个任务暂时无法完成"),
        Check(type="not_contains", keywords=["编造"]),
    ).passed
    assert not run_check(
        _outcome(final_answer="我编造了一个结果"),
        Check(type="not_contains", keywords=["编造"]),
    ).passed


def test_not_contains_boundary_empty_declaration_fails_loud():
    # 没有声明任何禁词 = 什么也没断言 → 不得算通过
    result = run_check(_outcome(final_answer="随便"), Check(type="not_contains"))
    assert not result.passed
    assert result.kind == "not_contains"


# ── tools_called（含 / 不含 / 顺序） ──


def _log(*tools: str) -> list[dict]:
    return [{"tool": name, "status": "success"} for name in tools]


def test_tools_called_contains_mode_is_default_and_order_insensitive():
    outcome = _outcome(execution_log=_log("search", "fetch_page"))
    assert run_check(outcome, Check(type="tools_called", tools=["search"])).passed
    assert run_check(
        outcome, Check(type="tools_called", tools=["search", "fetch_page"])
    ).passed
    assert run_check(
        outcome, Check(type="tools_called", tools=["fetch_page", "search"])
    ).passed
    assert not run_check(
        outcome, Check(type="tools_called", tools=["browser_open"], mode="contains")
    ).passed


def test_tools_called_not_contains_mode():
    outcome = _outcome(execution_log=_log("weather"))
    assert run_check(
        outcome, Check(type="tools_called", tools=["search"], mode="not_contains")
    ).passed
    assert not run_check(
        outcome, Check(type="tools_called", tools=["weather"], mode="not_contains")
    ).passed


def test_tools_called_exact_sequence_is_ordered_subsequence():
    outcome = _outcome(
        execution_log=_log("browser_open", "fetch_page", "browser_snapshot")
    )
    # 相对顺序对即可（中间允许夹别的工具）
    assert run_check(
        outcome,
        Check(type="tools_called", tools=["browser_open", "browser_snapshot"], mode="exact_sequence"),
    ).passed
    # 顺序错 → 失败
    assert not run_check(
        outcome,
        Check(type="tools_called", tools=["browser_snapshot", "browser_open"], mode="exact_sequence"),
    ).passed


def test_tools_called_boundaries_fail_loud():
    # 没有任何工具调用记录 → contains 失败（没记录不等于没调用过）
    assert not run_check(
        _outcome(), Check(type="tools_called", tools=["search"])
    ).passed
    # 空期望列表 → 什么也没断言 → 失败
    assert not run_check(
        _outcome(execution_log=_log("search")), Check(type="tools_called", tools=[])
    ).passed
    assert not run_check(
        _outcome(execution_log=_log("search")), Check(type="tools_called")
    ).passed
    # 未知 mode → 失败 + 明确 detail（不是异常）
    result = run_check(
        _outcome(execution_log=_log("search")),
        Check(type="tools_called", tools=["search"], mode="typo"),
    )
    assert not result.passed
    assert "mode" in result.detail


def test_tools_called_accepts_the_plan_example_shape():
    """计划 §Task 1 的示例：harness 注入的扁平 ``tool_calls`` + 关键字别名。"""
    outcome = {"tool_calls": ["search", "fetch_page"], "final_answer": "..."}
    assert run_check(outcome, Check(type="tools_called", keywords=["search"])).passed
    assert run_check(outcome, Check(type="tools_called", tools=["fetch_page"])).passed
    assert not run_check(outcome, Check(type="tools_called", tools=["weather"])).passed


def test_tools_called_reads_execution_step_models_too():
    """execution_log 的元素既可能是 dict（graph.ainvoke 的返回）也可能是 ExecutionStep。"""
    from browsepilot.agent.state import ExecutionStep

    outcome = _outcome(execution_log=[ExecutionStep(index=0, tool="weather")])
    assert run_check(outcome, Check(type="tools_called", tools=["weather"])).passed


# ── tier_is / stop_reason_is ──


def test_tier_is_positive_and_negative():
    assert run_check(_outcome(tier="tier3"), Check(type="tier_is", expected="tier3")).passed
    assert not run_check(
        _outcome(tier="tier1"), Check(type="tier_is", expected="tier3")
    ).passed
    assert not run_check(_outcome(tier=""), Check(type="tier_is", expected="tier1")).passed


def test_stop_reason_is_positive_and_negative():
    assert run_check(
        _outcome(stop_reason="partial"), Check(type="stop_reason_is", expected="partial")
    ).passed
    assert not run_check(
        _outcome(stop_reason="completed"),
        Check(type="stop_reason_is", expected="partial"),
    ).passed
    # 缺失 stop_reason → 失败
    assert not run_check(
        _outcome(stop_reason=""), Check(type="stop_reason_is", expected="completed")
    ).passed


def test_tier_and_stop_reason_fail_loud_when_expected_is_missing():
    """缺 ``expected`` 的空声明必须 fail-loud（与 ``field`` 对称，P4 final review Minor）。

    实际值来自 outcome、键取不到时是 ``None``：漏写 ``expected`` 时 ``None == None`` 会**通过**
    —— "tier / stop_reason 取不到"正是失败运行最容易出现的形状，这条空断言恰好会在最不该通过的
    运行上通过（与 field 的 I1 是同一条原则：空声明不得算通过）。
    """
    for check_type in ("tier_is", "stop_reason_is"):
        # 键缺失（None）：旧行为 None == None → 通过
        missing = run_check(_outcome(tier=None, stop_reason=None), Check(type=check_type))
        assert not missing.passed, check_type
        assert "未声明 expected" in missing.detail, check_type
        # 数据里有值也照样是空声明 —— 修后必须由**守卫**（而不是"恰好不等"）判失败
        declared = run_check(_outcome(), Check(type=check_type))
        assert not declared.passed, check_type
        assert "未声明 expected" in declared.detail, check_type


# ── tokens_below ──


def test_tokens_below_under_and_over_budget():
    assert run_check(
        _outcome(token_usage={"total_tokens": 900}), Check(type="tokens_below", limit=1000)
    ).passed
    # 边界：恰好等于预算算通过（≤）
    assert run_check(
        _outcome(token_usage={"total_tokens": 1000}), Check(type="tokens_below", limit=1000)
    ).passed
    assert not run_check(
        _outcome(token_usage={"total_tokens": 5000}), Check(type="tokens_below", limit=1000)
    ).passed


def test_tokens_below_accepts_usage_info_and_flat_shapes():
    assert run_check(
        _outcome(usage={"total_tokens": 10}), Check(type="tokens_below", limit=100)
    ).passed
    assert run_check(_outcome(total_tokens=10), Check(type="tokens_below", limit=100)).passed


def test_tokens_below_fails_loud_without_token_data_or_limit():
    # v2 的 AgentState 目前不携带 token 统计 → 没有数据必须 FAIL 且说清楚
    no_data = run_check(_outcome(), Check(type="tokens_below", limit=1000))
    assert not no_data.passed
    assert "token" in no_data.detail
    # 非法数值（bool / 字符串）视同没有数据
    assert not run_check(
        _outcome(total_tokens=True), Check(type="tokens_below", limit=1000)
    ).passed
    # 缺 limit → 失败
    assert not run_check(_outcome(total_tokens=1), Check(type="tokens_below")).passed


# ── latency_below ──


def test_latency_below_prefers_outcome_duration_ms():
    assert run_check(
        _outcome(duration_ms=1200.5), Check(type="latency_below", limit_ms=2000)
    ).passed
    assert not run_check(
        _outcome(duration_ms=2500), Check(type="latency_below", limit_ms=2000)
    ).passed
    # 边界：恰好等于预算算通过（≤）
    assert run_check(
        _outcome(duration_ms=2000), Check(type="latency_below", limit_ms=2000)
    ).passed


def test_latency_below_sums_execution_log_step_durations():
    log = _log("a", "b")
    log[0]["duration_ms"] = 100
    log[1]["duration_ms"] = 250
    assert run_check(
        _outcome(execution_log=log), Check(type="latency_below", limit_ms=400)
    ).passed
    assert not run_check(
        _outcome(execution_log=log), Check(type="latency_below", limit_ms=300)
    ).passed


def test_latency_below_fails_loud_without_timing_data():
    # 步骤没有 duration_ms / 根本没有 execution_log / 缺 limit_ms → FAIL（不能按 0ms 通过）
    assert not run_check(
        _outcome(execution_log=[{"tool": "a"}]), Check(type="latency_below", limit_ms=1000)
    ).passed
    assert not run_check(_outcome(), Check(type="latency_below", limit_ms=1000)).passed
    assert not run_check(
        _outcome(duration_ms=1.0), Check(type="latency_below")
    ).passed


# ── degradation_absent / needs_human ──


def test_degradation_absent_positive_and_negative():
    assert run_check(_outcome(), Check(type="degradation_absent")).passed
    failed = run_check(
        _outcome(degradation_log=[{"strategy": "weather", "error": "not_found"}]),
        Check(type="degradation_absent"),
    )
    assert not failed.passed
    assert "1" in failed.detail  # detail 里给出条数，便于定位


def test_needs_human_positive_and_negative():
    assert run_check(
        _outcome(pending_human_action=True), Check(type="needs_human")
    ).passed
    assert not run_check(
        _outcome(pending_human_action=False), Check(type="needs_human")
    ).passed
    # 缺字段 = 没有待人工动作
    assert not run_check({"tier": "tier3"}, Check(type="needs_human")).passed


# ── 未知类型 + run_eval 兼容层 ──


def test_unknown_check_type_fails_loud_not_raises():
    result = run_check(_outcome(), Check(type="no_such_check"))
    assert not result.passed
    assert "no_such_check" in result.detail
    assert result.kind == "no_such_check"


def test_run_eval_check_outcome_still_delegates_and_keeps_tuple_shape():
    from eval.run_eval import check_outcome

    outcome = _outcome(result_data={"city": "北京"})
    ok, detail = check_outcome(
        outcome, Check(type="field", field="city", expected="北京")
    )
    assert ok
    assert "city" in detail
    not_ok, _ = check_outcome(outcome, Check(type="field", field="city", expected="上海"))
    assert not not_ok
