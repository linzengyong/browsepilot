import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

import eval.scenarios as es
from browsepilot.agent.tracer import JsonlTracer
from browsepilot.model.messages import Message
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


# ── schema v2（P4-T2）：tags / mode / timeout / fragile / enabled / follow_up ──
#
# 校验原则是 fail-loud：tag / mode / 字段名的笔误必须在**加载时**爆出来
# （静默忽略 = 少跑一条场景 / 少断言一条，评测就变成自我欺骗）。


def _minimal(**overrides) -> dict:
    """一条最小合法场景（一个不为空的断言）。"""
    base: dict = {
        "id": "t-minimal",
        "name": "minimal",
        "prompt": "北京今天天气怎么样？",
        "checks": [{"type": "contains_keyword", "keywords": ["北京"]}],
    }
    base.update(overrides)
    return base


def test_scenario_schema_defaults():
    scenario = es.Scenario(**_minimal())
    assert scenario.tags == ["acceptance"]  # 缺省 = acceptance（P4-D2）
    assert scenario.mode == "both"
    assert scenario.timeout_seconds == 120.0
    assert scenario.fragile is False
    assert scenario.enabled is True
    assert scenario.follow_up == []


@pytest.mark.parametrize("tag", es.KNOWN_TAGS)
def test_known_tags_are_accepted(tag):
    assert es.Scenario(**_minimal(tags=[tag])).tags == [tag]


def test_unknown_tag_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        es.Scenario(**_minimal(tags=["smok"]))  # 拼错
    assert "smok" in str(excinfo.value)


def test_unknown_mode_is_rejected():
    with pytest.raises(ValidationError):
        es.Scenario(**_minimal(mode="browser"))


def test_unknown_field_is_rejected():
    """``mdoe: real`` 这种笔误不得被静默忽略（否则场景会在 mock 下被跑/被跳过得莫名其妙）。"""
    with pytest.raises(ValidationError):
        es.Scenario(**_minimal(mdoe="real"))


def test_scenario_without_checks_is_rejected():
    """没有断言的场景 = 必然通过的场景（虚假通过），拒绝。"""
    with pytest.raises(ValidationError):
        es.Scenario(**_minimal(checks=[]))


def test_blank_prompt_is_rejected():
    with pytest.raises(ValidationError):
        es.Scenario(**_minimal(prompt="   "))


def _write(tmp_path: Path, name: str, scenario: dict) -> Path:
    import yaml

    path = tmp_path / name
    path.write_text(yaml.safe_dump(scenario, allow_unicode=True), encoding="utf-8")
    return path


def test_load_error_names_the_file(tmp_path):
    _write(tmp_path, "broken.yaml", _minimal(tags=["nope"]))
    with pytest.raises(es.ScenarioLoadError) as excinfo:
        es.load_scenarios(tmp_path)
    assert "broken.yaml" in str(excinfo.value)


def test_load_rejects_duplicate_ids(tmp_path):
    _write(tmp_path, "a.yaml", _minimal())
    _write(tmp_path, "b.yaml", _minimal())
    with pytest.raises(es.ScenarioLoadError) as excinfo:
        es.load_scenarios(tmp_path)
    assert "t-minimal" in str(excinfo.value)
    assert "a.yaml" in str(excinfo.value) and "b.yaml" in str(excinfo.value)


def test_load_rejects_empty_directory(tmp_path):
    """空目录 = 0 条场景 = 空跑全绿：必须报错而不是静默通过。"""
    with pytest.raises(es.ScenarioLoadError):
        es.load_scenarios(tmp_path)


def test_load_accepts_v2_fields(tmp_path):
    _write(
        tmp_path,
        "s.yaml",
        _minimal(
            tags=["hitl", "browser"],
            mode="real",
            timeout_seconds=30.5,
            fragile=True,
            enabled=False,
            follow_up=["那明天呢？"],
        ),
    )
    (scenario,) = es.load_scenarios(tmp_path)
    assert scenario.tags == ["hitl", "browser"]
    assert scenario.mode == "real"
    assert scenario.timeout_seconds == 30.5
    assert scenario.fragile is True
    assert scenario.enabled is False
    assert scenario.follow_up == ["那明天呢？"]


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


def _mock_runnable() -> list[es.Scenario]:
    """mock 运行下真正会被执行的场景（测试对 aclose 次数的期望来源，独立于 run_eval 的实现）。"""
    return [
        s
        for s in es.load_scenarios(SCENARIOS_DIR)
        if s.enabled and s.mode in ("both", "mock")
    ]


def test_run_all_releases_the_page_after_every_scenario(tmp_path):
    """C2③：逐场景隔离 —— **跑过的**每个场景都要归还当前页（跳过/停用的场景不碰页面）。"""
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
    assert len(events) == len(_mock_runnable())


def test_run_all_teardown_runs_even_when_a_scenario_raises(tmp_path, monkeypatch):
    """逐场景隔离必须异常安全：场景炸了也要归还页面（否则下一个场景续在脏页上）。"""
    from eval import run_eval

    events: list[str] = []

    async def teardown() -> None:
        events.append("teardown")

    class BoomProvider(FakeProvider):
        async def chat(self, messages, *, model=None):
            raise RuntimeError("model exploded")

    # 第一条场景走 all_strategies_failed（不调 chat，不炸）→ 证明炸的是第二条、且两条都归还了
    _write_dir(
        tmp_path / "scenarios",
        _minimal(id="a-quiet", prompt="亚特兰蒂斯今天天气怎么样？"),
        _minimal(id="b-boom"),
    )
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", tmp_path / "scenarios")
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
    assert events == ["teardown", "teardown"]


def test_real_mode_wires_browser_pool_and_isolates_each_scenario(tmp_path, monkeypatch):
    """P4 关键输入 + C2③：real 模式必须注入浏览器池（不注入则每个 tier3 场景都
    browser_unavailable），并且每个场景后 aclose()。"""
    from eval import run_eval

    events: list[str] = []
    monkeypatch.setattr(
        run_eval, "_real_stack", lambda: (FakeProvider(), _SpyExecutor(events), _SpyPool(events))
    )
    tracer = JsonlTracer(trace_id="eval-real", out_dir=tmp_path)
    # --tag smoke：tier3/HITL 场景需要真浏览器/人工，这里只验证接线与隔离
    assert asyncio.run(run_eval._run_real(tracer, tags=["smoke"])) == 0
    assert events[0] == "start"
    assert events[-1] == "close"
    assert events.count("close") == 1
    smoke = [
        s
        for s in es.load_scenarios(SCENARIOS_DIR)
        if s.enabled and "smoke" in s.tags and s.mode in ("both", "real")
    ]
    assert events.count("aclose") == len(smoke)


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
    assert asyncio.run(run_eval._run_real(tracer, tags=["smoke"])) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "browser_unavailable" in out
    assert "chromium rev 1217 not found" in out
    assert "attach:none" in events  # executor 不留下未启动的池
    assert "close" not in events  # 没启动成功就不假装关闭
    assert events.count("aclose") == 7  # smoke 集（degrade 是 mock-only，real 运行跳过它）


# ── P4-T2：mode / tag / enabled 选择语义 + fragile 与 exit code 分离 ──


def _scenario(**overrides) -> es.Scenario:
    return es.Scenario(**_minimal(**overrides))


def _write_dir(directory: Path, *scenarios: dict) -> Path:
    """把场景字典写成 ``<dir>/<id>.yaml``（run_all 的真实输入形状）。"""
    import yaml

    directory.mkdir(parents=True, exist_ok=True)
    for scenario in scenarios:
        (directory / f"{scenario['id']}.yaml").write_text(
            yaml.safe_dump(scenario, allow_unicode=True), encoding="utf-8"
        )
    return directory


def test_select_scenarios_skips_real_only_in_mock():
    """mock 的 fake 依赖没有浏览器：real-only 场景是**跑不了**，不是失败。"""
    from eval import run_eval

    both = _scenario(id="both")
    real_only = _scenario(id="real-only", mode="real")
    mock_only = _scenario(id="mock-only", mode="mock")
    selection = run_eval.select_scenarios([both, real_only, mock_only], mode="mock")
    assert [s.id for s in selection.runnable] == ["both", "mock-only"]
    assert [s.id for s, _ in selection.skipped] == ["real-only"]
    assert "real" in selection.skipped[0][1]
    assert selection.disabled == []


def test_select_scenarios_skips_mock_only_in_real():
    from eval import run_eval

    selection = run_eval.select_scenarios([_scenario(id="mock-only", mode="mock")], mode="real")
    assert selection.runnable == []
    assert [s.id for s, _ in selection.skipped] == ["mock-only"]


def test_select_scenarios_disabled_is_disabled_not_skipped():
    """停用比“这次模式不匹配”更根本：mock 的日志里也要能看出它被停用了。"""
    from eval import run_eval

    disabled = _scenario(id="off", mode="real", enabled=False)
    selection = run_eval.select_scenarios([disabled], mode="mock")
    assert selection.disabled == [disabled]
    assert selection.runnable == [] and selection.skipped == []


def test_select_scenarios_tag_filter_keeps_any_matching_tag():
    from eval import run_eval

    hitl = _scenario(id="login", tags=["hitl", "browser"])
    smoke = _scenario(id="weather", tags=["smoke", "acceptance"])
    selection = run_eval.select_scenarios([hitl, smoke], mode="real", tags=["hitl"])
    assert [s.id for s in selection.runnable] == ["login"]
    assert [s.id for s, _ in selection.skipped] == ["weather"]


def test_select_scenarios_rejects_unknown_mode():
    """mode 拼错不得静默跑成全量（fail-loud）。"""
    from eval import run_eval

    with pytest.raises(ValueError):
        run_eval.select_scenarios([_scenario(id="x")], mode="mokc")


async def test_run_all_reports_skip_and_disabled_without_counting_them(
    tmp_path, monkeypatch, capsys
):
    from eval import run_eval

    _write_dir(
        tmp_path / "scenarios",
        _minimal(id="a-ok"),
        _minimal(id="b-real-only", mode="real"),
        _minimal(id="c-off", enabled=False),
    )
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", tmp_path / "scenarios")
    tracer = JsonlTracer(trace_id="eval-select", out_dir=tmp_path)
    code = await run_all(provider=FakeProvider(), executor=FakeExecutor(), tracer=tracer)
    out = capsys.readouterr().out
    assert code == 0
    assert "[PASS] a-ok" in out
    assert "[SKIP] b-real-only" in out and "mode=real" in out
    assert "[DISABLED] c-off" in out
    assert "[FAIL]" not in out
    assert "passed=1 failed=0" in out and "skipped=1 disabled=1" in out


async def test_run_all_tag_filter_runs_only_matching(tmp_path, monkeypatch, capsys):
    from eval import run_eval

    _write_dir(
        tmp_path / "scenarios",
        _minimal(id="a-smoke", tags=["smoke"]),
        _minimal(id="b-acceptance", tags=["acceptance"]),
    )
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", tmp_path / "scenarios")
    tracer = JsonlTracer(trace_id="eval-tag", out_dir=tmp_path)
    code = await run_all(
        provider=FakeProvider(), executor=FakeExecutor(), tracer=tracer, tags=["smoke"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "[PASS] a-smoke" in out
    assert "[SKIP] b-acceptance" in out


async def test_fragile_failure_does_not_gate_but_is_still_reported(tmp_path, monkeypatch, capsys):
    """D6：内容漂移（fragile）不该让门禁变红，但报告里必须看得见它失败了。"""
    from eval import run_eval

    _write_dir(
        tmp_path / "scenarios",
        _minimal(
            id="a-fragile",
            fragile=True,
            checks=[{"type": "contains_keyword", "keywords": ["NOPE"]}],
        ),
    )
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", tmp_path / "scenarios")
    tracer = JsonlTracer(trace_id="eval-fragile", out_dir=tmp_path)
    code = await run_all(provider=FakeProvider(), executor=FakeExecutor(), tracer=tracer)
    out = capsys.readouterr().out
    assert code == 0  # 不计入门禁
    assert "[FAIL] a-fragile" in out and "fragile=true" in out
    assert "fragile_failed=1" in out and "failed=0" in out


async def test_hard_failure_gates(tmp_path, monkeypatch):
    from eval import run_eval

    _write_dir(
        tmp_path / "scenarios",
        _minimal(id="a-hard", checks=[{"type": "contains_keyword", "keywords": ["NOPE"]}]),
    )
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", tmp_path / "scenarios")
    tracer = JsonlTracer(trace_id="eval-hard", out_dir=tmp_path)
    assert await run_all(provider=FakeProvider(), executor=FakeExecutor(), tracer=tracer) == 1


async def test_run_scenario_injects_tool_calls(tmp_path):
    """v2 的 AgentState 不写 execution_log：工具名由 harness 注入 outcome["tool_calls"]，
    否则 tools_called 断言在 mock 下全是假失败。"""
    from eval import run_eval

    scenario = _scenario(
        id="search",
        prompt="搜索一下 Python 3.13 正式发布时间",
        checks=[{"type": "tools_called", "tools": ["search"]}],
    )
    tracer = JsonlTracer(trace_id="eval-tools", out_dir=tmp_path)
    outcome, checks = await run_eval._run_scenario(
        scenario, provider=FakeProvider(), executor=FakeExecutor(), tracer=tracer
    )
    assert outcome["tool_calls"] == ["search"]
    assert all(passed for passed, _ in checks)


# ── 场景集（真实文件）：清单 / 覆盖度 / mock 门禁 ──


def _all_scenarios() -> list[es.Scenario]:
    return es.load_scenarios(SCENARIOS_DIR)


def test_scenario_set_matches_the_p4_inventory():
    """P4-T2 的场景清单（tier1/2/3 + hitl + multi-turn + degrade + 3 个 P0 场景）。"""
    expected = {
        "p0-weather",
        "p0-search",
        "p0-read-page",
        "t1_weather_beijing",
        "t1_weather_shanghai",
        "t1_weather_not_found",
        "t2_search_structural",
        "t2_search_content",
        "t2_read_page",
        "t2_read_page_failure",
        "t3_browser_open",
        "t3_browser_snapshot",
        "t3_browser_interaction",
        "hitl_login_detect",
        "multi_turn_context",
        "degrade_browser_unavailable",
    }
    assert {s.id for s in _all_scenarios()} == expected


def test_scenario_set_declares_modes_tags_and_flags():
    by_id = {s.id: s for s in _all_scenarios()}
    real_only = (
        "t3_browser_open",
        "t3_browser_snapshot",
        "t3_browser_interaction",
        "hitl_login_detect",
        "multi_turn_context",
    )
    for scenario_id in real_only:  # 真浏览器 / 人工 / 多轮 harness：mock 下跑不了
        assert by_id[scenario_id].mode == "real", scenario_id
    assert by_id["degrade_browser_unavailable"].mode == "mock"  # 只能在无浏览器环境下为真
    for scenario_id in ("p0-weather", "t1_weather_beijing", "t2_search_structural"):
        assert by_id[scenario_id].mode == "both", scenario_id
    assert {"hitl", "browser"} <= set(by_id["hitl_login_detect"].tags)
    assert by_id["degrade_browser_unavailable"].tags == ["degrade"]
    assert "browser" in by_id["t3_browser_open"].tags
    assert by_id["t3_browser_interaction"].enabled is False  # P3 遗留：交互链未入图
    assert by_id["multi_turn_context"].follow_up == ["那明天呢？"]  # schema 已支持多轮
    assert by_id["t1_weather_not_found"].tags == ["smoke", "acceptance"]


def test_p0_scenarios_are_still_runnable_and_p0_search_is_fragile():
    """向后兼容：P0 三场景仍在，只补了 tags/mode；内容断言的 p0-search 标 fragile。"""
    by_id = {s.id: s for s in _all_scenarios()}
    for scenario_id in ("p0-weather", "p0-search", "p0-read-page"):
        assert by_id[scenario_id].enabled and by_id[scenario_id].mode == "both"
    assert by_id["p0-search"].fragile is True
    assert by_id["t2_search_content"].fragile is True  # 内容断言版
    assert by_id["t2_search_structural"].fragile is False  # 结构断言版（D6）


def test_scenario_assertions_only_use_check_types_the_engine_knows():
    """YAML 写错 check 类型 → 运行时 FAIL（checks.py 的 fail-loud）；这里提前钉住不写错。"""
    from eval.checks import _CHECKS

    unknown = sorted(
        {
            check.type
            for scenario in _all_scenarios()
            for check in scenario.checks
            if check.type not in _CHECKS
        }
    )
    assert unknown == []


def test_every_scenario_prompt_routes_to_its_declared_tier():
    """离线验证提示词真的落在声明的层级上（不需要模型/浏览器）：

    - tier3 场景必须命中 browser_task 规则（否则 tier_is/browser_open 断言无从谈起）
    - tier2 场景必须命中 read_page（任务里带 URL / 打开类动词）
    - tier1 场景必须命中 weather/search

    这是提示词工程的回归网：改一个动词就可能把 tier3 任务抦给 read_page
    （“打开”/“访问”/URL 都比 browser_task 规则优先）。
    """
    from browsepilot.agent.router import classify_by_rules

    for scenario in _all_scenarios():
        decision = classify_by_rules(scenario.prompt)
        assert decision is not None, f"{scenario.id}: 规则层无法分类（会落到模型分类）"
        assert decision.tier == scenario.tier, f"{scenario.id}: {decision.tier} != {scenario.tier}"


def test_mock_run_is_green_over_the_real_scenario_set(capsys, monkeypatch):
    """P4-D8 门禁：mock 全绿，且明细真实 —— real-only 场景 SKIP（不是 FAIL）、停用场景列出不跑、
    每条跑过的场景都有数据来源（tools_called/城市名/URL 都来自 fake 的真实行为）。"""
    monkeypatch.setattr("sys.argv", ["run_eval", "--mock"])
    assert main() == 0
    out = capsys.readouterr().out
    runnable = _mock_runnable()
    assert "[FAIL]" not in out
    assert out.count("[PASS]") == len(runnable)
    for scenario in runnable:
        assert f"[PASS] {scenario.id}" in out
    for scenario in _all_scenarios():
        if scenario.mode == "real" and scenario.enabled:
            assert f"[SKIP] {scenario.id}" in out and "mode=real" in out
        if not scenario.enabled:
            assert f"[DISABLED] {scenario.id}" in out
    assert "fragile_failed=0" in out


# ── fake 契约（断言可信度的地基）：mock 下的 PASS 必须来自 fake 的**真实行为** ──


async def test_fake_weather_fails_loudly_for_unknown_city():
    """不存在的城市必须报 city_not_found —— 否则 t1_weather_not_found 的"不得编造"是假断言。"""
    result = await FakeExecutor().call("weather", {"city": "亚特兰蒂斯"})
    assert result.status == "error"
    assert result.error_type == "city_not_found"
    assert "°C" not in result.content  # 没有坐标就没有温度


async def test_fake_weather_returns_the_city_it_was_asked_for():
    result = await FakeExecutor().call("weather", {"city": "上海"})
    assert result.status == "success"
    assert result.data["city"] == "上海"


async def test_fake_browser_tools_are_unavailable_like_a_pool_less_executor():
    """mock 环境没有浏览器（= AppToolExecutor(browser=None)）：tier3 工具统一 browser_unavailable。"""
    executor = FakeExecutor()
    for tool in ("browser_open", "browser_snapshot", "browser_click"):
        result = await executor.call(tool, {})
        assert result.status == "error", tool
        assert result.error_type == "browser_unavailable", tool


async def test_fake_search_and_fetch_fail_like_the_real_services():
    executor = FakeExecutor()
    no_results = await executor.call("search", {"query": "亚特兰蒂斯今天天气怎么样？"})
    assert no_results.status == "error" and no_results.error_type == "service_error"
    hits = await executor.call("search", {"query": "Python 3.13 正式发布时间"})
    assert hits.status == "success" and hits.data["results"][0]["url"].startswith("https://")
    bad_fetch = await executor.call("fetch_page", {"url": "https://nope.invalid/x"})
    assert bad_fetch.status == "error" and bad_fetch.error_type == "service_error"
    page = await executor.call("fetch_page", {"url": "https://www.python.org"})
    assert page.status == "success" and "Python" in page.data["text"]


async def test_fake_provider_answers_from_the_data_payload():
    """answer 节点的语义是"基于给定数据回答"：mock 的 PASS 必须来自数据面本身。"""
    provider = FakeProvider()
    search = await provider.chat(
        [
            Message(
                role="user",
                content='数据：{"results": [{"title": "T", "url": "https://e.com/x", "snippet": "S"}]}',
            )
        ]
    )
    assert "https://e.com/x" in search.text
    weather = await provider.chat(
        [Message(role="user", content='数据：{"city": "上海", "temperature_c": 24.0}')]
    )
    assert "上海" in weather.text and "24.0" in weather.text
    # 没有数据面（chitchat / knowledge_qa 直答路径）→ 固定罐头文本（P0 起沿用）
    assert (await provider.chat([Message(role="user", content="你好")])).text == (
        "Python 3.13 于 2024 年发布"
    )


async def test_fake_provider_extracts_params_from_the_task_text():
    from browsepilot.agent.params import ReadPageParams, WeatherParams

    provider = FakeProvider()
    city = await provider.structured(
        [Message(role="user", content="上海今天天气怎么样？")], schema=WeatherParams
    )
    assert city.city == "上海"
    page = await provider.structured(
        [Message(role="user", content="打开 https://www.python.org 告诉我页面主要内容")],
        schema=ReadPageParams,
    )
    assert page.url == "https://www.python.org"  # 回归：正则不得把 www.python.org 截成 www
    bare = await provider.structured(
        [Message(role="user", content="用浏览器看看 example.com 页面上有什么内容")],
        schema=ReadPageParams,
    )
    assert bare.url == "https://example.com"  # 域名补 scheme（模型按 url 描述产出的形状）
