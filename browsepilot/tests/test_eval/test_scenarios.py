import asyncio
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


async def test_run_all_mock(tmp_path):
    tracer = JsonlTracer(trace_id="eval", out_dir=tmp_path)
    code = await run_all(provider=FakeProvider(), executor=FakeExecutor(), tracer=tracer)
    assert code == 0


# ── P4 harness 接线：real 模式浏览器注入 + 逐场景隔离（P3 final review C2③） ──


class _SpyExecutor(FakeExecutor):
    """记录 aclose() 的 FakeExecutor（逐场景隔离的释放点，P4 界面 = AppToolExecutor.aclose）。"""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def aclose(self) -> None:
        self.events.append("aclose")

    def attach_browser(self, browser) -> None:
        self.events.append(f"attach:{'pool' if browser is not None else 'none'}")


class _SpyPool:
    """记录 start/close 的 BrowserPool 替身（绝不起真浏览器）；可注入启动失败。"""

    def __init__(self, events: list[str], *, start_error: Exception | None = None) -> None:
        self.events = events
        self.start_error = start_error

    async def start(self) -> None:
        self.events.append("start")
        if self.start_error is not None:
            raise self.start_error

    async def close(self) -> None:
        self.events.append("close")


def test_run_all_releases_the_page_after_every_scenario(tmp_path):
    """C2③：逐场景隔离 —— 每个场景跑完都要归还当前页（场景之间不共享页面）。"""
    events: list[str] = []

    async def teardown() -> None:
        events.append("teardown")

    tracer = JsonlTracer(trace_id="eval", out_dir=tmp_path)
    code = asyncio.run(
        run_all(
            provider=FakeProvider(), executor=FakeExecutor(), tracer=tracer, teardown=teardown
        )
    )
    assert code == 0
    assert len(events) == len(es.load_scenarios(SCENARIOS_DIR))


def test_run_all_teardown_runs_even_when_a_scenario_raises(tmp_path):
    """逐场景隔离必须异常安全：场景炸了也要归还页面（否则下一个场景续在脏页上）。"""
    events: list[str] = []

    async def teardown() -> None:
        events.append("teardown")

    class BoomProvider(FakeProvider):
        async def chat(self, messages, *, model=None):
            raise RuntimeError("model exploded")

    tracer = JsonlTracer(trace_id="eval", out_dir=tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(
            run_all(
                provider=BoomProvider(),
                executor=FakeExecutor(),
                tracer=tracer,
                teardown=teardown,
            )
        )
    assert events == ["teardown"]


def test_real_mode_wires_browser_pool_and_isolates_each_scenario(tmp_path, monkeypatch):
    """P4 关键输入 + C2③：real 模式必须注入浏览器池（不注入则每个 tier3 场景都
    browser_unavailable），并且每个场景后 aclose()。"""
    from eval import run_eval

    events: list[str] = []
    monkeypatch.setattr(
        run_eval, "_real_stack", lambda: (FakeProvider(), _SpyExecutor(events), _SpyPool(events))
    )
    tracer = JsonlTracer(trace_id="eval-real", out_dir=tmp_path)
    assert asyncio.run(run_eval._run_real(tracer)) == 0
    assert events[0] == "start"
    assert events[-1] == "close"
    assert events.count("close") == 1
    assert events.count("aclose") == len(es.load_scenarios(SCENARIOS_DIR))


def test_real_mode_warns_when_the_browser_cannot_start(tmp_path, monkeypatch, capsys):
    """池起不来（本机没装 chromium）时打警告 + Tier3 降级：环境问题不能被读成任务失败，
    Tier1/2 仍照常评测。"""
    from eval import run_eval

    events: list[str] = []
    monkeypatch.setattr(
        run_eval,
        "_real_stack",
        lambda: (
            FakeProvider(),
            _SpyExecutor(events),
            _SpyPool(events, start_error=RuntimeError("chromium rev 1217 not found")),
        ),
    )
    tracer = JsonlTracer(trace_id="eval-real-fail", out_dir=tmp_path)
    assert asyncio.run(run_eval._run_real(tracer)) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "browser_unavailable" in out
    assert "chromium rev 1217 not found" in out
    assert "attach:none" in events  # executor 不留下未启动的池
    assert "close" not in events  # 没启动成功就不假装关闭
    assert events.count("aclose") == len(es.load_scenarios(SCENARIOS_DIR))
