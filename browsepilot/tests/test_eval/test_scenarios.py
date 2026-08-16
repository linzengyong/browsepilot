from pathlib import Path

import pytest
from pydantic import ValidationError

import eval.scenarios as es
from browsepilot.agent.tracer import JsonlTracer
from eval.fake_deps import FakeExecutor, FakeProvider
from eval.run_eval import check_outcome, main, run_all
from eval.scenarios import Check

SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent / "eval" / "scenarios"


def test_scenario_files_load_and_validate():
    scenarios = es.load_scenarios(SCENARIOS_DIR)
    ids = {s.id for s in scenarios}
    assert {"p0-weather", "p0-search", "p0-read-page"} <= ids
    assert all(s.prompt for s in scenarios)
    assert all(s.tier in ("tier1", "tier2", "tier3") for s in scenarios)


def test_scenario_requires_prompt():
    with pytest.raises(ValidationError):
        es.Scenario(id="x", name="x")


def test_run_eval_smoke(capsys, monkeypatch):
    # --mock keeps the smoke test deterministic (no network / no tokens)
    monkeypatch.setattr("sys.argv", ["run_eval", "--mock"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "scenarios loaded" in out
    assert "p0-weather" in out


# FakeProvider/FakeExecutor 定义在 eval/fake_deps.py（run_eval --mock 与测试共用）：
# FakeProvider.chat 返回 ChatResult(text="Python 3.13 于 2024 年发布")；structured 按 schema 字段
# 给 city=北京/query=q/url=https://example.com；FakeExecutor.call 对 weather 返回
# data={"city": "北京", "temperature_c": 26.0}，对 search 返回 data={"results": []}，
# 对 fetch_page 返回 data={"text": "Python 主页"}，其余返回 success 空 data。


def test_check_field_expected():
    ok, _ = check_outcome(
        {"result_data": {"city": "北京"}}, Check(type="field", field="city", expected="北京")
    )
    assert ok


def test_check_field_number_kind():
    ok, _ = check_outcome(
        {"result_data": {"temperature_c": 26.0}},
        Check(type="field", field="temperature_c", kind="number"),
    )
    assert ok
    ok2, _ = check_outcome(
        {"result_data": {"temperature_c": "hot"}},
        Check(type="field", field="temperature_c", kind="number"),
    )
    assert not ok2


def test_check_contains_keyword():
    ok, _ = check_outcome(
        {"final_answer": "发布于 2024 年"}, Check(type="contains_keyword", keywords=["2024"])
    )
    assert ok


def test_run_all_mock(tmp_path):
    tracer = JsonlTracer(trace_id="eval", out_dir=tmp_path)
    code = run_all(provider=FakeProvider(), executor=FakeExecutor(), tracer=tracer)
    assert code == 0
