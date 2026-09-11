"""P4-T4: quantitative stats + dual-format reports (``eval/report.py``).

三段：

- **A 聚合**（纯函数，不碰 IO）：已知结果集 → 精确断言；成功率的分母 = **实际执行**的样本
  （跳过/停用不算）；P50/P95 的边界（单样本、双样本插值、空集）；token/cost 的"未提供 ≠ 0"
  （采集不到就缺席，报表里的 0 只可能是真的 0）；fragile 失败单独计数（不进 ``failed``）。
- **B 渲染**：JSON（环境行 + 汇总 + 分层 + 每轮 + 跨轮方差 + flaky）与 Markdown
  （环境行 + 汇总表 + 分层表 + 场景表四种状态 + 方差/flaky 段 + 单元格转义）；
  以及"未测到不等于 0"的渲染门控（token 方差行 / JSON null）、mock banner、池未启动警告。
- **C 接线**：``--json/--md/--repeat`` 端到端（fake 场景集 + mock 模式，离线）；
  exit code 语义不变（fragile 失败不 gate、非 fragile 失败 gate）；空跑不写报告；
  real 模式的池启动状态进环境行（I2）；token 覆盖度进结果行。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import eval.report as report
from browsepilot.agent.tracer import JsonlTracer
from eval import run_eval
from eval.fake_deps import FakeExecutor, FakeProvider
from eval.report import (
    DISABLED,
    FAIL,
    PASS,
    SKIP,
    Round,
    ScenarioResult,
    aggregate,
    environment,
    flaky_scenarios,
    git_commit,
    percentile,
    resolve_output_path,
    write_json,
    write_markdown,
)
from eval.run_eval import main

ENV = {
    "date": "2026-08-16T12:00:00+08:00",
    "mode": "mock",
    "model": "mock (FakeProvider)",
    "git_commit": "b86d5ed",
}


def _result(id: str, **overrides) -> ScenarioResult:
    """一条"跑过且通过"的结果；各测试只声明自己关心的字段。"""
    base: dict = {
        "id": id,
        "tier": "tier1",
        "tags": ("smoke",),
        "mode": "mock",
        "passed": True,
        "duration_ms": 10.0,
        "tokens": 100,
        "stop_reason": "completed",
    }
    base.update(overrides)
    return ScenarioResult(**base)


# ── A. 聚合（纯函数） ──


def test_scenario_result_status_covers_the_four_states():
    assert _result("p").status == PASS
    assert _result("f", passed=False, failure_detail="x").status == FAIL
    assert _result("s", passed=False, skipped=True, skip_reason="mode=real").status == SKIP
    assert _result("d", passed=False, disabled=True).status == DISABLED
    # 跳过/停用的行不可能"通过"（没跑过就没有通过可言）——防御性：即便标记打架也以跳过为准
    assert _result("x", passed=True, skipped=True).status == SKIP
    assert _result("y", skipped=True, disabled=True).status == DISABLED


def test_aggregate_known_result_set():
    """5 条已知结果 → 每个聚合数字都精确可验。"""
    results = [
        _result("a", duration_ms=10.0, tokens=100, cost_usd=0.001),
        _result("b", duration_ms=20.0, tokens=200, cost_usd=0.002),
        _result("c", passed=False, duration_ms=30.0, tokens=300, failure_detail="缺城市"),
        _result("d", passed=False, fragile=True, duration_ms=40.0, tokens=None),
        _result(
            "e",
            passed=False,
            skipped=True,
            skip_reason="mode=real",
            duration_ms=None,
            tokens=None,
            stop_reason=None,
        ),
    ]
    agg = aggregate(results)

    assert (agg.total, agg.passed, agg.failed, agg.fragile_failed) == (5, 2, 1, 1)
    assert (agg.skipped, agg.disabled, agg.executed) == (1, 0, 4)
    assert agg.success_rate == 0.5  # 2/4：分母是实际执行的 4 条（跳过的不算）
    # 延迟：sorted([10,20,30,40]) → P50 = 25（插值），P95 = 38.5
    assert (agg.latency_samples, agg.latency_ms_p50, agg.latency_ms_p95) == (4, 25.0, 38.5)
    assert (agg.latency_ms_min, agg.latency_ms_max) == (10.0, 40.0)
    assert (agg.total_tokens, agg.token_samples) == (600, 3)  # d 没采集到 → 不算样本、不填 0
    assert (agg.total_cost_usd, agg.cost_samples) == (0.003, 2)


def test_aggregate_single_sample_uses_the_sample_for_both_percentiles():
    """单样本边界：P50 == P95 == 唯一样本（不是抛异常，也不是 None）。"""
    agg = aggregate([_result("only", duration_ms=12.5, tokens=7, cost_usd=None)])
    assert (agg.latency_ms_p50, agg.latency_ms_p95) == (12.5, 12.5)
    assert (agg.latency_ms_min, agg.latency_ms_max, agg.latency_samples) == (12.5, 12.5, 1)
    assert agg.success_rate == 1.0
    assert (agg.total_tokens, agg.token_samples) == (7, 1)
    assert (agg.total_cost_usd, agg.cost_samples) == (0.0, 0)  # cost 缺席 ≠ 0 元


def test_aggregate_empty_set_is_defined_not_a_crash():
    agg = aggregate([])
    assert (agg.total, agg.executed, agg.passed, agg.failed) == (0, 0, 0, 0)
    assert agg.success_rate is None  # 没有样本 → 成功率未定义（不是 0%，也不是 100%）
    assert (agg.latency_ms_p50, agg.latency_ms_p95) == (None, None)
    assert (agg.latency_ms_min, agg.latency_ms_max, agg.latency_samples) == (None, None, 0)
    assert (agg.total_tokens, agg.token_samples) == (0, 0)
    assert (agg.total_cost_usd, agg.cost_samples) == (0.0, 0)
    assert agg.per_tier == {}


def test_aggregate_all_skipped_has_no_success_rate():
    results = [_result("a", passed=False, skipped=True), _result("b", passed=False, disabled=True)]
    agg = aggregate(results)
    assert (agg.executed, agg.success_rate) == (0, None)
    assert (agg.skipped, agg.disabled) == (1, 1)
    assert agg.latency_ms_p50 is None


def test_aggregate_per_tier_split():
    results = [
        _result("t1a", tier="tier1", duration_ms=10.0),
        _result("t1b", tier="tier1", passed=False, duration_ms=20.0),
        _result("t2a", tier="tier2", duration_ms=30.0, tokens=50),
        _result("t3a", tier="tier3", passed=False, skipped=True, tokens=None),
    ]
    agg = aggregate(results)
    assert list(agg.per_tier) == ["tier1", "tier2", "tier3"]  # 稳定顺序
    assert (agg.per_tier["tier1"].executed, agg.per_tier["tier1"].success_rate) == (2, 0.5)
    assert agg.per_tier["tier1"].latency_ms_p50 == 15.0
    assert (agg.per_tier["tier2"].success_rate, agg.per_tier["tier2"].total_tokens) == (1.0, 50)
    # 整层被跳过 → 该层没有可执行样本（不是 0% 成功率）
    assert agg.per_tier["tier3"].executed == 0
    assert agg.per_tier["tier3"].success_rate is None
    assert agg.per_tier["tier3"].skipped == 1


def test_aggregate_counts_fragile_failure_separately_from_gated_failure():
    """P4-D6：fragile 失败照实记 FAIL，但**不**混进 failed（门禁计数）。"""
    agg = aggregate(
        [
            _result("ok"),
            _result("drift", passed=False, fragile=True, failure_detail="ddgs 漂移"),
            _result("hard", passed=False, failure_detail="硬失败"),
        ]
    )
    assert (agg.passed, agg.failed, agg.fragile_failed, agg.executed) == (1, 1, 1, 3)
    assert agg.success_rate == pytest.approx(1 / 3, abs=1e-4)


def test_aggregate_treats_zero_tokens_as_a_real_sample():
    """0 与"没有数据"是两件事：0 计数进样本，缺席不进（fail-loud 的两面）。"""
    agg = aggregate(
        [_result("a", tokens=0, cost_usd=0.0), _result("b", tokens=None, cost_usd=None)]
    )
    assert (agg.total_tokens, agg.token_samples) == (0, 1)
    assert (agg.total_cost_usd, agg.cost_samples) == (0.0, 1)


def test_aggregate_total_is_conserved_when_skipped_and_disabled_overlap():
    """Minor（P4-T4 审查）：``skipped`` 与 ``disabled`` 同时为真时只算**停用**一次 ——
    ``total == executed + skipped + disabled`` 无条件成立（否则汇总行自己就对不上账）。"""
    results = [_result("a"), _result("x", passed=False, skipped=True, disabled=True)]
    agg = aggregate(results)
    assert (agg.total, agg.executed, agg.skipped, agg.disabled) == (2, 1, 0, 1)
    assert agg.total == agg.executed + agg.skipped + agg.disabled


def test_aggregate_sums_token_coverage_over_self_describing_samples():
    """token 总和要能被读成"N/M 调用有计量"（P4-T3 I2 的自描述数据面）。"""
    results = [
        _result("a", tokens=100, measured_calls=3, model_calls=4),
        _result("b", tokens=200, measured_calls=2, model_calls=2),
        _result("c", tokens=None),  # 没采集到 → 覆盖度同样是未知（不是 0/0）
    ]
    agg = aggregate(results)
    assert (agg.total_tokens, agg.token_samples) == (300, 2)
    assert (agg.measured_calls, agg.model_calls) == (5, 6)


def test_runstats_is_an_alias_of_aggregate():
    """计划（P4-D3）管这个函数叫 ``RunStats``，实现叫 ``aggregate``：别名零成本兼容。"""
    assert report.RunStats is report.aggregate

@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([10.0, 20.0], 0.0, 10.0),
        ([10.0, 20.0], 0.5, 15.0),
        ([10.0, 20.0], 0.95, 19.5),
        ([10.0, 20.0], 1.0, 20.0),
        ([5.0], 0.5, 5.0),
        ([5.0], 0.95, 5.0),
        ([], 0.5, None),
    ],
)
def test_percentile_interpolates_and_handles_the_boundaries(values, p, expected):
    assert percentile(values, p) == expected


def test_percentile_rejects_out_of_range_probability():
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


# ── B. 渲染：JSON ──


def test_write_json_renders_environment_summary_tiers_and_rows(tmp_path):
    results = [
        _result("a", tier="tier1", tags=("smoke",), duration_ms=10.0, tokens=100),
        _result(
            "b",
            tier="tier2",
            tags=("acceptance", "browser"),
            passed=False,
            duration_ms=20.0,
            tokens=None,
            stop_reason="all_strategies_failed",
            failure_detail="答案缺少 上海",
        ),
    ]
    path = write_json(aggregate(results), results, tmp_path / "report.json", environment=ENV)
    assert path == tmp_path / "report.json"

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["environment"] == ENV
    assert data["summary"]["success_rate"] == 0.5
    assert data["summary"]["executed"] == 2
    assert (data["summary"]["passed"], data["summary"]["failed"]) == (1, 1)
    assert (data["summary"]["total_tokens"], data["summary"]["token_samples"]) == (100, 1)
    assert set(data["per_tier"]) == {"tier1", "tier2"}
    assert data["per_tier"]["tier2"]["success_rate"] == 0.0

    # rounds 缺省 = 把 results 当作单轮（P4-D3/D7 的单轮报告形状）
    assert [item["index"] for item in data["rounds"]] == [1]
    rows = data["rounds"][0]["scenarios"]
    assert [row["id"] for row in rows] == ["a", "b"]
    assert [row["status"] for row in rows] == [PASS, FAIL]
    assert {
        "id",
        "tier",
        "tags",
        "mode",
        "passed",
        "skipped",
        "disabled",
        "fragile",
        "duration_ms",
        "tokens",
        "cost_usd",
        "stop_reason",
        "failure_detail",
    } <= set(rows[0])
    assert rows[1]["failure_detail"] == "答案缺少 上海"
    assert rows[1]["stop_reason"] == "all_strategies_failed"
    assert "variance" not in data and "flaky" not in data  # 单轮没有"跨轮"可言


def test_write_json_keeps_utf8_readable(tmp_path):
    results = [_result("a", passed=False, failure_detail="答案缺少 上海")]
    text = write_json(aggregate(results), results, tmp_path / "r.json").read_text(encoding="utf-8")
    assert "答案缺少 上海" in text  # ensure_ascii=False：报告是给人看的
    assert text.endswith("\n")


def test_write_json_with_repeats_reports_variance_and_flaky(tmp_path):
    round_one = [_result("a", duration_ms=10.0), _result("b", duration_ms=20.0)]
    round_two = [
        _result("a", duration_ms=30.0, passed=False, failure_detail="翻车"),
        _result("b", duration_ms=40.0),
    ]
    rounds = [Round(1, round_one), Round(2, round_two)]
    pooled = round_one + round_two
    data = json.loads(
        write_json(
            aggregate(pooled), pooled, tmp_path / "r.json", environment=ENV, rounds=rounds
        ).read_text(encoding="utf-8")
    )

    assert [item["index"] for item in data["rounds"]] == [1, 2]
    assert [item["stats"]["success_rate"] for item in data["rounds"]] == [1.0, 0.5]
    assert [len(item["scenarios"]) for item in data["rounds"]] == [2, 2]
    assert data["summary"]["executed"] == 4  # 跨轮 = 样本池（每轮各算一遍）

    rate = data["variance"]["success_rate"]
    assert (rate["min"], rate["max"], rate["mean"], rate["stdev"]) == (0.5, 1.0, 0.75, 0.25)
    latency = data["variance"]["latency_ms_p50"]
    assert (latency["min"], latency["max"], latency["mean"], latency["stdev"]) == (
        15.0,
        35.0,
        25.0,
        10.0,
    )
    assert data["variance"]["total_tokens"]["stdev"] == 0.0  # 两轮各 200，没有波动
    assert data["flaky"] == [{"id": "a", "statuses": [PASS, FAIL]}]


def test_write_json_never_renders_unmeasured_totals_as_zero(tmp_path):
    """I1：两轮都没有 usage → ``total_tokens`` / ``total_cost_usd`` 写 ``null``，不写 0。

    同一份报告里汇总段写"—（未提供）"、方差段写 0，是自相矛盾的（0 只可能是真的 0）。
    """
    round_one = [_result("a", tokens=None, cost_usd=None, duration_ms=10.0)]
    round_two = [_result("a", tokens=None, cost_usd=None, duration_ms=20.0)]
    rounds = [Round(1, round_one), Round(2, round_two)]
    pooled = round_one + round_two
    data = json.loads(
        write_json(
            aggregate(pooled), pooled, tmp_path / "r.json", environment=ENV, rounds=rounds
        ).read_text(encoding="utf-8")
    )

    assert data["summary"]["total_tokens"] is None  # 未测到 → null（不是 0）
    assert data["summary"]["total_cost_usd"] is None
    assert (data["summary"]["token_samples"], data["summary"]["cost_samples"]) == (0, 0)
    assert all(item["stats"]["total_tokens"] is None for item in data["rounds"])
    assert "total_tokens" not in data["variance"]  # 没测到就不进方差（不渲染 0/0/0/0）
    assert set(data["variance"]) == {"success_rate", "latency_ms_p50", "latency_ms_p95"}


def test_write_json_keeps_measured_totals_and_labels_the_stdev_basis(tmp_path):
    """测到了就给数字，并且写明 ``stdev`` 是**总体**标准差（pstdev）：数字要被引用。"""
    round_one = [_result("a", tokens=100, cost_usd=0.001, duration_ms=10.0)]
    round_two = [_result("a", tokens=300, cost_usd=0.003, duration_ms=30.0)]
    rounds = [Round(1, round_one), Round(2, round_two)]
    pooled = round_one + round_two
    data = json.loads(
        write_json(aggregate(pooled), pooled, tmp_path / "r.json", rounds=rounds).read_text(
            "utf-8"
        )
    )

    assert data["variance"]["total_tokens"] == {
        "min": 100.0,
        "max": 300.0,
        "mean": 200.0,
        "stdev": 100.0,
        "stdev_kind": "population",
    }
    assert data["summary"]["total_tokens"] == 400
    assert data["summary"]["token_samples"] == 2


def test_write_json_variance_skips_rounds_without_token_samples(tmp_path):
    """缺席的一轮不进方差：把"没测到"当 0 会把 min 拉成 0，而读者无从察觉。"""
    round_one = [_result("a", tokens=None)]
    round_two = [_result("a", tokens=100)]
    rounds = [Round(1, round_one), Round(2, round_two)]
    pooled = round_one + round_two
    data = json.loads(
        write_json(aggregate(pooled), pooled, tmp_path / "r.json", rounds=rounds).read_text(
            "utf-8"
        )
    )

    assert data["variance"]["total_tokens"]["min"] == 100.0  # 只有测到的那一轮进样本
    assert data["variance"]["total_tokens"]["mean"] == 100.0


def test_write_json_marks_the_summary_scope_when_pooled_over_rounds(tmp_path):
    """顶层标口径：重复 N 轮时 ``summary`` 是**样本池**（每轮各计一遍），不是单次运行。"""
    round_one = [_result("a")]
    round_two = [_result("a")]
    rounds = [Round(1, round_one), Round(2, round_two)]
    pooled = round_one + round_two
    data = json.loads(
        write_json(aggregate(pooled), pooled, tmp_path / "r.json", rounds=rounds).read_text(
            "utf-8"
        )
    )
    assert data["summary_scope"] == "pooled_over_rounds"

    single = [_result("a")]
    one = json.loads(
        write_json(aggregate(single), single, tmp_path / "one.json").read_text(encoding="utf-8")
    )
    assert "summary_scope" not in one  # 单轮没有"跨轮样本池"这回事（与 variance/flaky 同规则）


def test_write_json_rows_carry_token_coverage(tmp_path):
    """行级覆盖度：token 数要能读成"N/M 调用有计量"（未计量的调用没进总和 → 下限）。"""
    results = [_result("a", tokens=42, measured_calls=2, model_calls=5)]
    data = json.loads(
        write_json(aggregate(results), results, tmp_path / "r.json").read_text(encoding="utf-8")
    )
    row = data["rounds"][0]["scenarios"][0]
    assert (row["tokens"], row["measured_calls"], row["model_calls"]) == (42, 2, 5)


def test_write_json_to_directory_creates_a_timestamped_file(tmp_path):
    results = [_result("a")]
    target = write_json(aggregate(results), results, tmp_path / "reports")
    assert target.parent == tmp_path / "reports"  # 父目录自动创建
    assert re.fullmatch(r"\d{8}-\d{6}\.json", target.name)
    assert json.loads(target.read_text(encoding="utf-8"))["summary"]["passed"] == 1


def test_write_markdown_to_directory_creates_a_timestamped_file(tmp_path):
    results = [_result("a")]
    target = write_markdown(aggregate(results), results, tmp_path / "reports")
    assert target.parent == tmp_path / "reports"
    assert re.fullmatch(r"\d{8}-\d{6}\.md", target.name)


def test_resolve_output_path_rules(tmp_path):
    stamp = "20260816-120000"
    # 已存在目录 / 结尾分隔符 / 无后缀 → 目录（P4-D4：<timestamp>.json + <timestamp>.md）
    (tmp_path / "reports").mkdir()
    assert resolve_output_path(tmp_path / "reports", suffix=".json", stamp=stamp) == (
        tmp_path / "reports" / "20260816-120000.json"
    )
    assert resolve_output_path(tmp_path / "new-dir", suffix=".md", stamp=stamp) == (
        tmp_path / "new-dir" / "20260816-120000.md"
    )
    assert resolve_output_path(f"{tmp_path}/reports/", suffix=".md", stamp=stamp) == (
        tmp_path / "reports" / "20260816-120000.md"
    )
    # 带后缀 → 文件路径（原样）
    assert resolve_output_path(tmp_path / "r.json", suffix=".json", stamp=stamp) == (
        tmp_path / "r.json"
    )


def test_resolve_output_path_avoids_overwriting_a_same_second_report(tmp_path):
    """Minor：同一秒内的第二次运行不能覆盖第一次的报告（冲突时加 ``-1`` / ``-2`` 后缀）。"""
    stamp = "20260911-190513"
    (tmp_path / f"{stamp}.json").write_text("{}", encoding="utf-8")
    assert resolve_output_path(tmp_path, suffix=".json", stamp=stamp) == (
        tmp_path / f"{stamp}-1.json"
    )
    (tmp_path / f"{stamp}-1.json").write_text("{}", encoding="utf-8")
    assert resolve_output_path(tmp_path, suffix=".json", stamp=stamp) == (
        tmp_path / f"{stamp}-2.json"
    )
    # 另一个后缀不受影响：一对 JSON/MD 在同一秒仍用同一戳（成对可找）
    assert resolve_output_path(tmp_path, suffix=".md", stamp=stamp) == tmp_path / f"{stamp}.md"


def test_timestamp_format_is_filename_safe():
    stamp = report.timestamp(datetime(2026, 8, 16, 12, 0, 5, tzinfo=UTC))
    assert stamp == "20260816-120005"
    assert re.fullmatch(r"\d{8}-\d{6}", stamp)


# ── B. 渲染：Markdown ──


def test_write_markdown_renders_env_summary_tiers_and_every_status(tmp_path):
    results = [
        _result("a-pass", tier="tier1", tags=("smoke",), duration_ms=10.0, tokens=100),
        _result(
            "b-fail",
            tier="tier2",
            tags=("acceptance",),
            passed=False,
            duration_ms=20.0,
            tokens=None,
            stop_reason="all_strategies_failed",
            failure_detail="答案缺少 上海",
        ),
        _result(
            "c-skip",
            tier="tier3",
            tags=("browser",),
            passed=False,
            skipped=True,
            skip_reason="mode=real（本次运行 = mock）",
            duration_ms=None,
            tokens=None,
            stop_reason=None,
        ),
        _result(
            "d-off",
            tier="tier3",
            tags=("hitl",),
            passed=False,
            disabled=True,
            duration_ms=None,
            tokens=None,
            stop_reason=None,
        ),
    ]
    text = write_markdown(
        aggregate(results), results, tmp_path / "r.md", environment=ENV
    ).read_text(encoding="utf-8")

    # 环境行（日期 / 模式 / 模型 / git）
    assert ENV["date"] in text
    assert "mock (FakeProvider)" in text
    assert "b86d5ed" in text
    assert "模式: mock" in text

    # 场景表：四种状态各一行，列序 = id/tier/tags/结果/耗时/tokens/stop_reason/明细
    assert "| a-pass | tier1 | smoke | PASS | 10.0 | 100 | completed | — |" in text
    assert (
        "| b-fail | tier2 | acceptance | FAIL | 20.0 | — | all_strategies_failed | 答案缺少 上海 |"
        in text
    )
    assert "| c-skip | tier3 | browser | SKIP | — | — | — | mode=real（本次运行 = mock） |" in text
    assert "| d-off | tier3 | hitl | DISABLED | — | — | — | — |" in text

    # 汇总：成功率的分子/分母写清楚（2 条执行、1 条通过）
    assert "成功率" in text
    assert "50.0% (1/2)" in text
    assert "fragile" in text

    # 分层表：tier3 全跳过 → 没有可执行样本
    tier1_line = next(line for line in text.splitlines() if line.startswith("| tier1 |"))
    assert "100.0% (1/1)" in tier1_line
    tier3_line = next(line for line in text.splitlines() if line.startswith("| tier3 |"))
    assert "无可执行样本" in tier3_line


def test_write_markdown_marks_missing_tokens_and_cost_as_unavailable(tmp_path):
    """采集不到 token/cost → 报表写"未提供"，不写 0（0 只可能是真的 0）。"""
    results = [_result("a", tokens=None, cost_usd=None)]
    text = write_markdown(aggregate(results), results, tmp_path / "r.md").read_text("utf-8")
    assert "| 总 token | —（未提供） |" in text
    assert "| 总成本 (USD) | —（未提供） |" in text


def test_write_markdown_escapes_pipes_and_newlines_in_details(tmp_path):
    results = [_result("a", passed=False, failure_detail="左 | 右\n换行")]
    text = write_markdown(aggregate(results), results, tmp_path / "r.md").read_text("utf-8")
    assert r"左 \| 右 换行" in text  # 竖线转义 + 换行压平：表格不被撑破
    row = next(line for line in text.splitlines() if line.startswith("| a |"))
    assert row.count("|") - row.count(r"\|") == 9  # 8 列 → 9 个真实分隔符


def test_write_markdown_with_repeats_renders_rounds_variance_and_flaky(tmp_path):
    round_one = [_result("a", duration_ms=10.0), _result("b", duration_ms=20.0)]
    round_two = [
        _result("a", duration_ms=30.0, passed=False, failure_detail="翻车"),
        _result("b", duration_ms=40.0),
    ]
    rounds = [Round(1, round_one), Round(2, round_two)]
    pooled = round_one + round_two
    text = write_markdown(
        aggregate(pooled), pooled, tmp_path / "r.md", environment=ENV, rounds=rounds
    ).read_text(encoding="utf-8")

    assert "### 第 1 轮 / 2" in text and "### 第 2 轮 / 2" in text
    assert "跨轮波动" in text
    assert "2 轮样本池" in text  # 汇总口径写明白：跨轮是样本池（每轮各计一遍）
    assert "| 成功率 | 0.5000 | 1.0000 | 0.7500 | 0.2500 |" in text
    assert "| 延迟 P50 (ms) | 15.00 | 35.00 | 25.00 | 10.00 |" in text
    assert "`a`: PASS, FAIL" in text  # flaky 单轮标注
    assert "`b`" not in text.split("flaky")[-1]  # b 两轮都过 → 不算 flaky


def test_write_markdown_variance_marks_unmeasured_token_row_as_unavailable(tmp_path):
    """I1：两轮都测不到 token → 方差表那行写"—（未提供）"，不是 0（真实产物曾渲染 0）。"""
    round_one = [_result("a", tokens=None, duration_ms=10.0)]
    round_two = [_result("a", tokens=None, duration_ms=20.0)]
    rounds = [Round(1, round_one), Round(2, round_two)]
    pooled = round_one + round_two
    text = write_markdown(
        aggregate(pooled), pooled, tmp_path / "r.md", environment=ENV, rounds=rounds
    ).read_text(encoding="utf-8")

    variance_section = text.split("跨轮波动", 1)[1]  # 只看方差段（汇总段也有"总 token"行）
    row = next(line for line in variance_section.splitlines() if line.startswith("| 总 token |"))
    assert "—（未提供）" in row
    assert "| 0 |" not in row  # 没有"0"可言：一个样本都没有
    # 汇总段的同一指标也写"未提供"（两段不能一个说 0、一个说没有）
    assert "| 总 token | —（未提供） |" in text


def test_write_markdown_variance_shows_token_numbers_when_measured(tmp_path):
    """测到 token 就正常出数字（门控只在该指标没有样本时生效）。"""
    round_one = [_result("a", tokens=100)]
    round_two = [_result("a", tokens=300)]
    rounds = [Round(1, round_one), Round(2, round_two)]
    pooled = round_one + round_two
    text = write_markdown(
        aggregate(pooled), pooled, tmp_path / "r.md", environment=ENV, rounds=rounds
    ).read_text(encoding="utf-8")

    assert "| 总 token | 100 | 300 | 200 | 100 |" in text
    variance_section = text.split("跨轮波动", 1)[1]
    assert "—（未提供）" not in variance_section  # 测到了就不写"未提供"（成本段不在这里）


def test_write_markdown_labels_the_variance_stdev_as_population(tmp_path):
    """Minor：``stdev`` 用 ``statistics.pstdev``（总体）→ 表头写明，读者不用猜口径。"""
    rounds = [
        Round(1, [_result("a", duration_ms=10.0)]),
        Round(2, [_result("a", duration_ms=30.0)]),
    ]
    pooled = [result for round_ in rounds for result in round_.results]
    text = write_markdown(
        aggregate(pooled), pooled, tmp_path / "r.md", environment=ENV, rounds=rounds
    ).read_text(encoding="utf-8")

    assert "| 指标 | 最小 | 最大 | 均值 | 标准差（总体） |" in text
    assert "| 延迟 P50 (ms) | 10.00 | 30.00 | 20.00 | 10.00 |" in text  # pstdev([10,30]) = 10


def test_write_markdown_mock_mode_carries_a_banner(tmp_path):
    """Minor：mock 的数字不代表模型质量，报告要自带这句话（文章引用时不会被误读）。"""
    results = [_result("a")]
    mock_text = write_markdown(
        aggregate(results), results, tmp_path / "m.md", environment=ENV
    ).read_text(encoding="utf-8")
    assert "本次为 mock 数据（FakeProvider/FakeExecutor），不代表模型质量" in mock_text

    real_env = {**ENV, "mode": "real", "model": "gpt-4o-mini"}
    real_text = write_markdown(
        aggregate(results), results, tmp_path / "r.md", environment=real_env
    ).read_text(encoding="utf-8")
    assert "不代表模型质量" not in real_text


def test_write_markdown_warns_when_the_browser_pool_is_unavailable(tmp_path):
    """I2：池起不来要显眼地进报告 —— 否则 tier3 的 0% 会被读成任务失败。"""
    env = {**ENV, "mode": "real", "browser_pool": report.BROWSER_POOL_UNAVAILABLE}
    results = [
        _result("t3-browser", tier="tier3", passed=False, failure_detail="browser_unavailable")
    ]
    text = write_markdown(
        aggregate(results), results, tmp_path / "r.md", environment=env
    ).read_text(encoding="utf-8")

    assert "浏览器池: unavailable (start failed)" in text  # 环境行如实写
    warning = next(line for line in text.splitlines() if "环境警告" in line)
    assert "browser_unavailable" in warning
    assert "归因" in warning  # 失败归因：环境，不是任务
    assert "tier3" in warning  # 具体到受影响的层

    ok_env = {**ENV, "mode": "real", "browser_pool": report.BROWSER_POOL_STARTED}
    ok_text = write_markdown(
        aggregate(results), results, tmp_path / "ok.md", environment=ok_env
    ).read_text(encoding="utf-8")
    assert "浏览器池: started" in ok_text
    assert "环境警告" not in ok_text


def test_write_markdown_summary_reads_the_token_total_as_a_lower_bound(tmp_path):
    """Minor：token 总和带上"N/M 调用有计量"，读者知道这是下限而不是全量。"""
    results = [_result("a", tokens=100, measured_calls=3, model_calls=6)]
    text = write_markdown(aggregate(results), results, tmp_path / "r.md").read_text("utf-8")
    assert "3/6 调用有计量" in text
    assert "下限" in text

    complete = [_result("a", tokens=100, measured_calls=2, model_calls=2)]
    complete_text = write_markdown(aggregate(complete), complete, tmp_path / "c.md").read_text(
        "utf-8"
    )
    assert "2/2 调用有计量" in complete_text
    assert "下限" not in complete_text  # 全覆盖就不说"下限"


def test_write_markdown_single_round_has_no_variance_section(tmp_path):
    results = [_result("a")]
    text = write_markdown(aggregate(results), results, tmp_path / "r.md").read_text("utf-8")
    assert "跨轮波动" not in text
    assert "flaky" not in text
    assert "样本池" not in text  # 单轮没有"跨轮样本池"这种说法


# ── B. 环境行 / flaky 工具函数 ──


def test_environment_carries_date_mode_model_and_git_commit():
    env = environment(
        mode="mock",
        model="mock (FakeProvider)",
        when=datetime(2026, 8, 16, 12, 0, 5, tzinfo=UTC),
        extra={"repeats": 2, "tags": ["smoke"], "note": None},
    )
    assert env["date"].startswith("2026-08-16T12:00:05")
    assert (env["mode"], env["model"]) == ("mock", "mock (FakeProvider)")
    assert env["repeats"] == 2 and env["tags"] == ["smoke"]
    assert "note" not in env  # None 的额外项不写进报告
    assert env["git_commit"] == git_commit()
    assert env["git_commit"] is None or re.fullmatch(r"[0-9a-f]{4,40}", env["git_commit"])


def test_git_commit_is_none_when_git_is_unavailable(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(report.subprocess, "run", boom)
    assert git_commit() is None


def test_git_commit_is_none_outside_a_repository(monkeypatch):
    class _Proc:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository"

    monkeypatch.setattr(report.subprocess, "run", lambda *args, **kwargs: _Proc())
    assert git_commit(Path("not-a-repo")) is None


def _skipped(id: str) -> ScenarioResult:
    return _result(id, passed=False, skipped=True, duration_ms=None, tokens=None, stop_reason=None)


def test_flaky_scenarios_flags_only_inconsistent_verdicts():
    rounds = [
        Round(1, [_result("flaky", passed=False), _result("stable"), _skipped("skip")]),
        Round(2, [_result("flaky"), _result("stable"), _skipped("skip")]),
    ]
    assert flaky_scenarios(rounds) == [{"id": "flaky", "statuses": [FAIL, PASS]}]
    assert flaky_scenarios(rounds[:1]) == []  # 单轮没有"跨轮不一致"


# ── C. 接线：--json / --md / --repeat（mock，离线） ──


def _minimal(**overrides) -> dict:
    base: dict = {
        "id": "t-minimal",
        "name": "minimal",
        "prompt": "北京今天天气怎么样？",
        "checks": [{"type": "contains_keyword", "keywords": ["北京"]}],
    }
    base.update(overrides)
    return base


def _write_dir(directory: Path, *scenarios: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for scenario in scenarios:
        (directory / f"{scenario['id']}.yaml").write_text(
            yaml.safe_dump(scenario, allow_unicode=True), encoding="utf-8"
        )
    return directory


def _tracer(tmp_path: Path, trace_id: str = "eval-report") -> JsonlTracer:
    return JsonlTracer(trace_id=trace_id, out_dir=tmp_path)


def _use_fake_scenarios(tmp_path, monkeypatch, *scenarios: dict) -> None:
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", _write_dir(tmp_path / "scenarios", *scenarios))
    monkeypatch.chdir(tmp_path)  # main() 的 JsonlTracer 写相对路径 data/traces


def test_main_writes_json_and_markdown_reports(tmp_path, monkeypatch):
    _use_fake_scenarios(
        tmp_path, monkeypatch, _minimal(id="a-ok"), _minimal(id="b-real-only", mode="real")
    )
    json_path, md_path = tmp_path / "out.json", tmp_path / "out.md"
    monkeypatch.setattr(
        "sys.argv",
        ["run_eval", "--mock", "--json", str(json_path), "--md", str(md_path)],
    )
    assert main() == 0

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["environment"]["mode"] == "mock"
    assert "browser_pool" not in data["environment"]  # mock 没有池，就不塞一个假状态
    assert data["environment"]["date"]  # 报告自带日期
    assert data["summary"]["passed"] == 1 and data["summary"]["skipped"] == 1
    assert [row["id"] for row in data["rounds"][0]["scenarios"]] == ["a-ok", "b-real-only"]
    assert [row["status"] for row in data["rounds"][0]["scenarios"]] == [PASS, SKIP]

    text = md_path.read_text(encoding="utf-8")
    assert "| a-ok |" in text and "| b-real-only |" in text
    assert "PASS" in text and "SKIP" in text


def test_main_writes_reports_into_a_directory_with_one_timestamp(tmp_path, monkeypatch, capsys):
    """``--md data/eval-reports/``（P4-D7 的调用形状）→ 目录下 <timestamp>.json/.md，且成对同戳。"""
    _use_fake_scenarios(tmp_path, monkeypatch, _minimal(id="a-ok"))
    reports = tmp_path / "eval-reports"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_eval",
            "--mock",
            "--json",
            str(reports) + "/",
            "--md",
            str(reports) + "/",
        ],
    )
    assert main() == 0
    written = sorted(p.name for p in reports.iterdir())
    assert len(written) == 2
    assert written[0].rsplit(".", 1)[0] == written[1].rsplit(".", 1)[0]  # 同一时间戳
    assert {p.suffix for p in reports.iterdir()} == {".json", ".md"}
    out = capsys.readouterr().out
    assert "[eval] report" in out  # 报告路径打进日志（跑完知道去哪找）


def test_main_without_report_flags_writes_nothing(tmp_path, monkeypatch):
    _use_fake_scenarios(tmp_path, monkeypatch, _minimal(id="a-ok"))
    monkeypatch.setattr("sys.argv", ["run_eval", "--mock"])
    assert main() == 0
    assert list(tmp_path.glob("*.json")) == []
    assert list(tmp_path.glob("*.md")) == []


async def test_repeat_runs_the_same_set_every_round_and_reports_each_round(tmp_path, monkeypatch):
    _use_fake_scenarios(tmp_path, monkeypatch, _minimal(id="a-ok"))
    seen: list[str] = []
    run_scenario = run_eval._run_scenario

    async def spy(scenario, **kwargs):
        seen.append(scenario.id)
        return await run_scenario(scenario, **kwargs)

    monkeypatch.setattr(run_eval, "_run_scenario", spy)
    json_path = tmp_path / "repeat.json"
    code = await run_eval.run_rounds(
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=_tracer(tmp_path),
        mode="mock",
        repeat=2,
        json_path=json_path,
        model="mock (FakeProvider)",
    )
    assert code == 0
    assert seen == ["a-ok", "a-ok"]  # 同一场景集跑了两轮

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert [item["index"] for item in data["rounds"]] == [1, 2]
    assert data["environment"]["repeats"] == 2
    assert all(item["stats"]["passed"] == 1 for item in data["rounds"])
    assert data["variance"]["success_rate"]["stdev"] == 0.0
    assert data["flaky"] == []


async def test_repeat_keeps_gate_and_fragile_semantics(tmp_path, monkeypatch):
    """exit code 语义不变：非 fragile 失败 gate（哪怕只有一轮失败）；fragile 失败只计数。"""
    _use_fake_scenarios(
        tmp_path,
        monkeypatch,
        _minimal(id="a-ok"),
        _minimal(id="b-hard", prompt="上海今天天气怎么样？"),  # 断言要"北京" → 非 fragile 硬失败
        _minimal(
            id="c-drift",
            prompt="上海今天天气怎么样？",
            fragile=True,
        ),
    )
    json_path = tmp_path / "gate.json"
    code = await run_eval.run_rounds(
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=_tracer(tmp_path),
        mode="mock",
        repeat=2,
        json_path=json_path,
    )
    assert code == 1  # b-hard 每一轮都失败 → gate
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["summary"]["failed"] == 2  # 两轮的 b-hard
    assert data["summary"]["fragile_failed"] == 2  # c-drift 只计数、不进 failed
    assert data["flaky"] == []  # 两轮表现一致 → 不是 flaky


async def test_repeat_rejects_non_positive_counts(tmp_path, monkeypatch):
    _use_fake_scenarios(tmp_path, monkeypatch, _minimal(id="a-ok"))
    with pytest.raises(ValueError, match="repeat"):
        await run_eval.run_rounds(
            provider=FakeProvider(),
            executor=FakeExecutor(),
            tracer=_tracer(tmp_path),
            mode="mock",
            repeat=0,
        )


def test_main_rejects_non_positive_repeat(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["run_eval", "--mock", "--repeat", "0"])
    with pytest.raises(SystemExit):
        main()
    assert "--repeat" in capsys.readouterr().err


def test_empty_selection_writes_no_report(tmp_path, monkeypatch, capsys):
    """空跑（--tag 选不中可运行场景）：既有语义是非零退出、且**没有**报告可写。"""
    json_path, md_path = tmp_path / "none.json", tmp_path / "none.md"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["run_eval", "--mock", "--tag", "browser", "--json", str(json_path), "--md", str(md_path)],
    )
    assert main() == 1
    assert "0 条可运行场景" in capsys.readouterr().out
    assert not json_path.exists() and not md_path.exists()


def test_empty_selection_in_repeat_mode_stops_after_the_first_round(tmp_path, monkeypatch, capsys):
    """空跑不重复 N 遍（没有数据可报，也没有必要再选一次）：日志里只报一次原因。"""
    md_path = tmp_path / "none.md"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["run_eval", "--mock", "--tag", "browser", "--repeat", "3", "--md", str(md_path)],
    )
    assert main() == 1
    out = capsys.readouterr().out
    assert out.count("0 条可运行场景") == 1
    assert "round 2/3" not in out
    assert not md_path.exists()


class _SpyExecutor(FakeExecutor):
    async def aclose(self) -> None:  # 逐场景隔离的释放点（real 模式 teardown）
        return None

    def attach_browser(self, browser) -> None:
        return None


class _SpyPool:
    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


async def test_real_mode_run_writes_a_report(tmp_path, monkeypatch):
    """real 分支同样接线报告（模型 id 进环境行）；报告模式 = real。"""
    monkeypatch.setattr(
        run_eval, "_real_stack", lambda: (FakeProvider(), _SpyExecutor(), _SpyPool())
    )
    json_path = tmp_path / "real.json"
    md_path = tmp_path / "real.md"
    code = await run_eval._run_real(
        _tracer(tmp_path, "eval-real-report"),
        tags=["smoke"],
        json_path=json_path,
        md_path=md_path,
    )
    assert code == 0
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["environment"]["mode"] == "real"
    assert data["environment"]["browser_pool"] == "started"  # 池起来了：环境行如实写
    assert data["summary"]["passed"] > 0
    text = md_path.read_text(encoding="utf-8")
    assert "浏览器池: started" in text
    assert "环境警告" not in text


class _PoolStartBoom(_SpyPool):
    async def start(self) -> None:
        raise RuntimeError("chromium rev 1217 not found")


async def test_real_mode_pool_start_failure_is_attributed_in_the_report(tmp_path, monkeypatch):
    """I2：池起不来 → 环境行 ``browser_pool=unavailable (start failed)`` + MD 显眼警告行。

    P4-D5 要求 tier3 的失败能**可见地**归因到环境；只有日志里的 WARNING 不够 ——
    报告被单独引用时看不出"tier3 0%"是环境问题。
    """
    monkeypatch.setattr(
        run_eval, "_real_stack", lambda: (FakeProvider(), _SpyExecutor(), _PoolStartBoom())
    )
    json_path, md_path = tmp_path / "real.json", tmp_path / "real.md"
    # tags=browser → 只选 real 模式的 tier3 场景：它们就是"池起不来时全炸"的那一批
    await run_eval._run_real(
        _tracer(tmp_path, "eval-real-no-pool"),
        tags=["browser"],
        json_path=json_path,
        md_path=md_path,
    )

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["environment"]["browser_pool"] == "unavailable (start failed)"
    tier3 = data["per_tier"]["tier3"]
    assert tier3["executed"] == 3  # tier3 确实跑了
    assert tier3["failed"] + tier3["fragile_failed"] >= 1  # 池没起来 → 这些失败归因环境
    text = md_path.read_text(encoding="utf-8")
    assert "浏览器池: unavailable (start failed)" in text
    warning = next(line for line in text.splitlines() if "环境警告" in line)
    assert "browser_unavailable" in warning
    assert "tier3" in warning  # 具体到受影响的层（含执行/失败条数）
    assert "归因" in warning  # 失败归因：环境，不是任务


def test_usage_coverage_reads_the_self_describing_snapshot():
    """``token_usage`` → ``(有计量的调用数, 模型调用总数)``（P4-T3 I2 的自描述字段）。"""
    assert run_eval._usage_coverage(
        {"total_tokens": 42, "chat_calls": 3, "estimated_calls": 1, "model_calls": 5}
    ) == (2, 5)
    assert run_eval._usage_coverage(
        {"total_tokens": 42, "chat_calls": 2, "model_calls": 2}
    ) == (2, 2)
    assert run_eval._usage_coverage({}) == (None, None)  # 快照缺席 → 未知，不是 0
    assert run_eval._usage_coverage({"total_tokens": 42, "chat_calls": 2}) == (
        None,
        None,
    )  # 没有诚实分母就不断言覆盖度


async def test_run_all_maps_usage_coverage_into_result_rows(tmp_path, monkeypatch):
    """行级覆盖度接线：token 总和因此可被读成"N/M 调用有计量"。"""
    _use_fake_scenarios(tmp_path, monkeypatch, _minimal(id="a-ok"))

    async def fake_run_scenario(scenario, **kwargs):
        return {
            "final_answer": "北京晴",
            "stop_reason": "completed",
            "duration_ms": 12.0,
            "tool_calls": [],
            "token_usage": {
                "total_tokens": 42,
                "usage_source": "chat_only",
                "chat_calls": 3,
                "estimated_calls": 1,
                "model_calls": 5,
            },
        }

    monkeypatch.setattr(run_eval.harness, "run_scenario", fake_run_scenario)
    results: list[ScenarioResult] = []
    code = await run_eval.run_all(
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=_tracer(tmp_path, "usage-coverage"),
        mode="mock",
        results=results,
    )

    assert code == 0
    assert (results[0].tokens, results[0].measured_calls, results[0].model_calls) == (42, 2, 5)
