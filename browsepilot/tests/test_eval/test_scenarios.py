import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

import eval.scenarios as es
from browsepilot.agent.tracer import JsonlTracer
from browsepilot.model.messages import ChatResult, Message
from eval.checks import run_check
from eval.fake_deps import FakeExecutor, FakeProvider
from eval.report import ScenarioResult, aggregate
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


def test_check_rejects_unknown_fields():
    """Check 同样 ``extra="forbid"``（P4-T2 审查 I1）：``keyword:``（漏 s）这类笔误必须报错
    —— 静默忽略 = 少断言一条，而日志上看不出来。"""
    with pytest.raises(ValidationError):
        es.Check(type="contains_keyword", keyword=["北京"])
    assert es.Check(type="contains_keyword", keywords=["北京"]).keywords == ["北京"]


def test_unknown_tier_is_rejected():
    """``tier`` 是路由的**实际**层级，只有三个合法值：拼错（``tier4``）不得静默落进场景集。"""
    with pytest.raises(ValidationError):
        es.Scenario(**_minimal(tier="tier4"))


def test_scenario_without_checks_is_rejected():
    """没有断言的场景 = 必然通过的场景（虚假通过），拒绝。"""
    with pytest.raises(ValidationError):
        es.Scenario(**_minimal(checks=[]))


@pytest.mark.parametrize("value", [0, 0.0, -1, -0.5, -120])
def test_non_positive_timeout_is_rejected(value):
    """P4-T3 审查 Minor：``timeout_seconds`` 是**正数预算** —— 0/负数是必然超时/语义未定义，
    必须在加载时报错，而不是把每条场景都跑成超时后才让人去猜。"""
    with pytest.raises(ValidationError):
        es.Scenario(**_minimal(timeout_seconds=value))


def test_load_rejects_non_positive_timeout(tmp_path):
    """同一约束在文件加载路径上也成立（带文件名定位）。"""
    _write(tmp_path, "s-zero.yaml", _minimal(timeout_seconds=0))
    with pytest.raises(es.ScenarioLoadError) as excinfo:
        es.load_scenarios(tmp_path)
    assert "s-zero.yaml" in str(excinfo.value)
    assert "timeout_seconds" in str(excinfo.value)


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
    # P4-T5：P0 基线已存档 —— 仍在输出里（列出为 [DISABLED]，报告要能解释"为什么没跑"），
    # 但不再执行、不再进统计
    assert "[DISABLED] p0-weather" in out
    assert "[PASS] t1_weather_beijing" in out


# FakeProvider/FakeExecutor 定义在 eval/fake_deps.py（run_eval --mock 与测试共用）。
# v0.4 起 fake 是**任务感知**的（不是固定罐头值）：FakeProvider.structured 按 schema 字段名从
# 任务文本取值（city="北京"/query=去掉搜索动词的任务原文/url=任务里的 URL 或补 scheme 的域名）；
# chat 从 answer 节点塞进 user message 的 ``数据：{json}`` 合成回答（搜索结果/天气/页面正文），
# 没有数据面（chitchat/knowledge_qa 直答）时才退回固定文本 "Python 3.13 于 2024 年发布"。
# FakeExecutor.call：weather 对已知城市（北京/上海）返回 data={"city", "temperature_c", …}、
# 其余 city_not_found；search 对认识的查询（含 python）返回 data={"results": [...]}、
# 其余 service_error；fetch_page 对 python.org / example.com 返回 data={"text", "title", …}、
# 其余 service_error；6 个浏览器工具一律 browser_unavailable（mock 没有浏览器）。
# 这些行为的契约测试在本文件尾部（"fake 契约"一节）。


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


class _HookProvider(FakeProvider):
    """按关键字注入故障的 FakeProvider：只在任务文本含 ``keyword`` 的轮次上抛出/卡住。

    逐场景隔离测试的故障源 —— "一条场景炸了"必须能精确定位到那一条，而不是整批一起炸；
    其余场景照常走 fake 的确定性路径（b-pass 的 PASS 才有意义）。
    """

    def __init__(
        self, keyword: str, *, error: Exception | None = None, delay: float | None = None
    ) -> None:
        self.keyword = keyword
        self.error = error
        self.delay = delay

    async def chat(self, messages, *, model=None):
        if any(self.keyword in message.content for message in messages):
            if self.delay is not None:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
        return await super().chat(messages, model=model)


def test_run_all_teardown_runs_even_when_a_scenario_raises(tmp_path, monkeypatch):
    """逐场景隔离必须异常安全：场景炸了也要归还页面（否则脏页留给下一个场景），
    而且下一个场景**照跑**（C1：单条异常不再中断整轮）。"""
    from eval import run_eval

    events: list[str] = []

    async def teardown() -> None:
        events.append("teardown")

    _write_dir(
        tmp_path / "scenarios",
        _minimal(id="a-quiet"),
        _minimal(id="b-boom", prompt="BOOM 北京今天天气怎么样？"),
    )
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", tmp_path / "scenarios")
    tracer = JsonlTracer(trace_id="eval", out_dir=tmp_path)
    assert (
        asyncio.run(
            run_all(
                provider=_HookProvider("BOOM", error=RuntimeError("model exploded")),
                executor=FakeExecutor(),
                tracer=tracer,
                teardown=teardown,
            )
        )
        == 1
    )
    assert events == ["teardown", "teardown"]


async def test_run_all_continues_after_a_failed_scenario(tmp_path, monkeypatch, capsys):
    """P4-D5：失败场景不中断后续 —— a-fail（断言失败）之后 b-pass 照跑，两条结果行都在。"""
    from eval import run_eval

    _write_dir(
        tmp_path / "scenarios",
        _minimal(id="a-fail", checks=[{"type": "contains_keyword", "keywords": ["NOPE"]}]),
        _minimal(id="b-pass"),
    )
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", tmp_path / "scenarios")
    tracer = JsonlTracer(trace_id="eval-iso-fail", out_dir=tmp_path)
    code = await run_all(provider=FakeProvider(), executor=FakeExecutor(), tracer=tracer)
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] a-fail" in out
    assert "[PASS] b-pass" in out
    assert "passed=1 failed=1" in out


async def test_run_all_continues_after_a_raising_scenario(tmp_path, monkeypatch, capsys):
    """C1（P4-T3 审查）：场景**异常**（模型/服务炸了）不得中止整轮 ——
    ``[FAIL] <id>: EXCEPTION <type>: <msg>``、计为 failed、继续下一条（P4-D5）。"""
    from eval import run_eval

    _write_dir(
        tmp_path / "scenarios",
        _minimal(id="a-raise", prompt="BOOM 北京今天天气怎么样？"),
        _minimal(id="b-pass"),
    )
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", tmp_path / "scenarios")
    tracer = JsonlTracer(trace_id="eval-iso-raise", out_dir=tmp_path)
    code = await run_all(
        provider=_HookProvider("BOOM", error=RuntimeError("model exploded")),
        executor=FakeExecutor(),
        tracer=tracer,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] a-raise: EXCEPTION RuntimeError: model exploded" in out
    assert "[PASS] b-pass" in out
    assert "passed=1 failed=1" in out


async def test_run_all_continues_after_a_scenario_times_out(tmp_path, monkeypatch, capsys):
    """C1（T7 的痛）：场景超时（harness 的 TimeoutError 冒泡）只废自己那一条 ——
    一次 120s 卡顿不得让整轮 acceptance 的剩余场景全部消失。"""
    from eval import run_eval

    _write_dir(
        tmp_path / "scenarios",
        _minimal(id="a-stall", prompt="SLOW 北京今天天气怎么样？", timeout_seconds=0.02),
        _minimal(id="b-pass"),
    )
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", tmp_path / "scenarios")
    tracer = JsonlTracer(trace_id="eval-iso-timeout", out_dir=tmp_path)
    code = await run_all(
        provider=_HookProvider("SLOW", delay=0.2),
        executor=FakeExecutor(),
        tracer=tracer,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert (
        "[FAIL] a-stall: EXCEPTION TimeoutError: scenario 'a-stall' 第 1/1 轮超过 "
        "timeout_seconds=0.02s" in out
    )
    assert "[PASS] b-pass" in out
    assert "passed=1 failed=1" in out


async def test_raising_fragile_scenario_does_not_gate_but_is_reported(
    tmp_path, monkeypatch, capsys
):
    """D6 与 C1 的交点：fragile 场景**异常**（例如 ddgs 网络抖动）同样不计入门禁 ——
    但照实打 ``[FAIL] … EXCEPTION …``（失败可见，不伪装成通过）。"""
    from eval import run_eval

    _write_dir(
        tmp_path / "scenarios",
        _minimal(id="a-fragile-boom", prompt="BOOM 北京今天天气怎么样？", fragile=True),
    )
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", tmp_path / "scenarios")
    tracer = JsonlTracer(trace_id="eval-fragile-raise", out_dir=tmp_path)
    code = await run_all(
        provider=_HookProvider("BOOM", error=RuntimeError("model exploded")),
        executor=FakeExecutor(),
        tracer=tracer,
    )
    out = capsys.readouterr().out
    assert code == 0  # fragile 不 gate（D6）
    assert "[FAIL] a-fragile-boom: EXCEPTION RuntimeError: model exploded" in out
    assert "fragile=true" in out and "fragile_failed=1" in out


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
    # smoke 集（degrade 是 mock-only、real 运行跳过它）：p0-weather 存档前是 7，现为 6
    assert events.count("aclose") == 6


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


def test_main_tag_with_zero_runnable_scenarios_exits_nonzero(capsys, monkeypatch):
    """P4-T2 审查 Minor：``--tag browser`` 在 mock 下选不到任何可运行场景（tier3/HITL 都是
    real-only）—— 必须非零退出，而不是打印 passed=0 failed=0 静默“通过”。"""
    monkeypatch.setattr("sys.argv", ["run_eval", "--mock", "--tag", "browser"])
    assert main() == 1
    out = capsys.readouterr().out
    assert "0 条可运行场景" in out
    assert "passed=" not in out  # 没有假装跑过一轮


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
    # p0-weather 已存档（enabled: false，见下节）：存档**不**改运行口径（mode/tags 原样保留）
    for scenario_id in ("p0-weather", "t1_weather_beijing", "t2_search_structural"):
        assert by_id[scenario_id].mode == "both", scenario_id
    assert {"hitl", "browser"} <= set(by_id["hitl_login_detect"].tags)
    assert by_id["degrade_browser_unavailable"].tags == ["degrade"]
    assert "browser" in by_id["t3_browser_open"].tags
    assert by_id["t3_browser_interaction"].enabled is False  # P3 遗留：交互链未入图
    assert by_id["multi_turn_context"].follow_up == ["那明天呢？"]  # schema 已支持多轮
    assert by_id["t1_weather_not_found"].tags == ["smoke", "acceptance"]


# ── p0-* 存档（P4-T5）：历史基线落 enabled: false，语义逐条迁移到新版场景 ──
#
# p0-weather / p0-search / p0-read-page 是 P1 验收"P0 场景 100% 可靠"的证据（文章引用，见
# docs/v2-p1-report.md），不能删文件；P4-T5 的做法是**存档**：enabled: false（列出为
# [DISABLED]、不进统计），断言逐条迁移到 t1_/t2_ 新版场景。
#
# 下面两张表就是迁移的**可执行**记录（不是注释里的承诺）：每条 p0 断言必须在**启用中**的
# 新版场景里找到承接者，否则"存档"就变成了"悄悄少断言"。

_P0_ARCHIVED = ("p0-weather", "p0-search", "p0-read-page")

#: p0 场景 id → 承接它的新版场景 id（逐个断言比对，见 ``_covers``）
_P0_SUCCEEDED_BY: dict[str, list[str]] = {
    # field city=北京 + field temperature_c(number)：两条都迁进 t1_weather_beijing
    "p0-weather": ["t1_weather_beijing"],
    # 拆成两版：结构断言（tools_called/degradation/stop_reason，不依赖 ddgs 内容）+
    # 内容断言（contains "2024"，fragile，P4-D6）
    "p0-search": ["t2_search_structural", "t2_search_content"],
    # contains "Python" + URL → tier2 read_page（新版另断言确实调了 fetch_page）
    "p0-read-page": ["t2_read_page"],
}


def _covers(successor: Check, archived: Check) -> bool:
    """承接断言是否覆盖被存档断言的**语义**（按 check 类型逐条比对，不靠注释）。

    只处理 p0-* 用到的两种类型；将来有别的类型进 p0 档案时，这里显式加分支（不得默认 True）。
    """
    if successor.type != archived.type:
        return False
    if archived.type == "field":
        if successor.field != archived.field:
            return False
        if archived.kind is not None:  # kind: number → 承接者也得是数值断言
            return successor.kind == archived.kind
        return successor.expected == archived.expected
    if archived.type in ("contains_keyword", "not_contains"):
        return set(archived.keywords or []) <= set(successor.keywords or [])
    return False


def test_p0_baselines_are_archived_not_deleted():
    """P1 的历史基线：文件保留（可手动启用做回归对比），但不再参与统计（enabled: false）。"""
    by_id = {s.id: s for s in _all_scenarios()}
    for scenario_id in _P0_ARCHIVED:
        assert scenario_id in by_id, scenario_id
        assert by_id[scenario_id].enabled is False, scenario_id
        assert by_id[scenario_id].mode == "both", scenario_id  # 存档不动运行口径
    assert by_id["p0-search"].fragile is True  # 内容断言（D6）—— 存档前就标了
    assert set(_P0_SUCCEEDED_BY) == set(_P0_ARCHIVED)  # 每条基线都要有承接关系


def test_p0_assertion_semantics_have_enabled_successors():
    """覆盖无缺口：p0-* 的**每一条**断言都能在启用中的新版场景里找到承接者。

    缺口实例（T5 之前真实存在）：p0-weather 的 ``field temperature_c kind: number`` 没有承接者 ——
    直接存档就丢了"温度必须是数值"这条数据断言。
    """
    by_id = {s.id: s for s in _all_scenarios()}
    for archived_id, successor_ids in _P0_SUCCEEDED_BY.items():
        successors = [by_id[sid] for sid in successor_ids]
        for successor in successors:
            assert successor.enabled, f"{archived_id} 的承接者 {successor.id} 必须启用"
        for check in by_id[archived_id].checks:
            covering = [s.id for s in successors if any(_covers(c, check) for c in s.checks)]
            assert covering, (
                f"{archived_id} 的断言 {check.type}"
                f"({check.field or check.kind or check.keywords}) 在 {successor_ids} 里没有承接者"
                " —— 存档 = 丢断言"
            )


def test_archived_baselines_leave_the_count_to_the_new_set():
    """存档后的计数口径（P4-T5）：mock 可执行 8 条（原 11）、停用 4 条（3 条 p0 + 交互链）。"""
    scenarios = _all_scenarios()
    runnable = [s for s in scenarios if s.enabled and s.mode in ("both", "mock")]
    assert len(scenarios) == 16  # 文件都还在（存档 ≠ 删除）
    assert not [s for s in runnable if s.id.startswith("p0-")]
    assert len(runnable) == 8
    assert {s.id for s in scenarios if not s.enabled} == {
        *_P0_ARCHIVED,
        "t3_browser_interaction",
    }


def test_search_content_version_is_fragile_and_structural_one_is_not():
    """D6 的拆分口径：内容断言（依赖 ddgs 返回）fragile，结构断言不 fragile。"""
    by_id = {s.id: s for s in _all_scenarios()}
    assert by_id["t2_search_content"].fragile is True
    assert by_id["t2_search_structural"].fragile is False


def test_structural_search_scenario_does_not_assert_on_answer_wording():
    """P4-T5 实测加固：真实模式里模型会"简洁回答/如实拒答"而不回显链接
    （1 次 eval smoke + 2 次探针，答案都不含 "http"）—— 结构版场景只允许断言
    **结构**（工具调用 / 无降级 / 终止原因），答案措辞类断言必须留在内容版（fragile）。

    没有这条回归网，"答案里要出现链接"这种措辞断言会被当成结构断言重新加回来，
    而这个场景存在的全部意义就是"不依赖措辞、真实模式稳定"（P4-D6）。
    """
    by_id = {s.id: s for s in _all_scenarios()}
    structural = by_id["t2_search_structural"]
    types = [check.type for check in structural.checks]
    assert "contains_keyword" not in types and "not_contains" not in types
    assert {"tools_called", "degradation_absent", "stop_reason_is"} <= set(types)
    assert by_id["t2_search_content"].fragile is True  # 措辞/内容断言归它（不 gate）


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


async def test_fragile_content_drift_is_counted_but_does_not_gate(tmp_path, capsys):
    """P4-T5 核对（任务书 Step 3）：真实场景集上的内容漂移只计 fragile_failed、不 gate exit code，
    且报告层单独计数（T4）——结构版场景（t2_search_structural）不受内容漂移影响。

    漂移构造：FakeProvider 的答案里把 "2024" 换成 "20XX" —— 复刻 P2 D1 / P4-D6 的现场
    （ddgs 返回里没有发布日期，模型诚实不捏造）。这是**最接近真实事故的合成形状**：
    工具链本身完全正常（search 被调用、无降级），只有内容断言落空。
    """

    class _DriftedAnswerProvider(FakeProvider):
        async def chat(self, messages, *, model=None):
            result = await super().chat(messages, model=model)
            return ChatResult(text=result.text.replace("2024", "20XX"))

    results: list[ScenarioResult] = []
    tracer = JsonlTracer(trace_id="eval-fragile-drift", out_dir=tmp_path)
    code = await run_all(
        provider=_DriftedAnswerProvider(),
        executor=FakeExecutor(),
        tracer=tracer,
        results=results,
    )
    out = capsys.readouterr().out
    assert code == 0  # 内容漂移不是回归（D6）
    assert "[FAIL] t2_search_content" in out and "fragile=true" in out
    assert "[PASS] t2_search_structural" in out  # 结构断言不依赖 ddgs 内容 → 不受影响
    assert "fragile_failed=1" in out and "failed=0" in out
    stats = aggregate(results)  # 报告层（T4）：fragile 失败不混进门禁 failed
    assert (stats.executed, stats.passed, stats.failed, stats.fragile_failed) == (8, 7, 0, 1)
    drifted = next(result for result in results if result.id == "t2_search_content")
    assert drifted.fragile and not drifted.passed and drifted.failure_detail


# ── C1 回归网：合成失败/partial outcome 不得让正例场景全绿 ──
#
# 背景（P4-T1+T2 审查 C1）：harness 记录的 ``tool_calls`` 是**尝试**（含失败），而失败模板的
# answer 会把用户输入原样带出来（search 兜底 "no results for query: '<任务原文>'"、navigate 失败
# "打开页面失败：<url>"）—— 只断言 tools_called / contains_keyword 的正例场景因此能在
# "策略链全败"或"只打开没观察"（partial）的运行上 PASS。实例：t3_browser_open 的 keywords
# 含小写 "example.com"，被 "打开页面失败：https://example.com…" 满足。
#
# 这个回归网把原则编码进 CI：对每条**正例**场景构造"全策略失败"与"partial"合成 outcome，
# 要求至少一项断言失败。合成 outcome 刻意复刻真实失败模板（消息回显任务文本/URL、
# tool_calls = 尝试过的工具），即**对断言最不利**的形状。

#: 失败路径场景：它们断言的**就是**"失败必须诚实"（失败文案 / 不编造 / 不抛栈），
#: 合成失败 outcome 本来就应当让它们通过 —— 不适用"失败不得全绿"这条属性。
_FAILURE_PATH_SCENARIOS = frozenset(
    {
        "t1_weather_not_found",  # 断言失败文案（不得编造温度）
        "t2_read_page_failure",  # 断言失败文案（不得抛栈/编造）
        "degrade_browser_unavailable",  # 断言无浏览器时的降级文案（mock-only）
    }
)

_TOOL_FAILURE_TEMPLATES = {
    # 与 executor/actions 的真实模板同形：失败消息回显用户输入（URL / 任务原文）
    "browser_open": "打开页面失败：{prompt}（net::ERR_NAME_NOT_RESOLVED）",
    "browser_snapshot": "还没有打开的页面：请先调用 browser_open(url) 再观察或操作",
}
_DEFAULT_FAILURE_TEMPLATE = "{tool} failed for: {prompt}"


def _positive_scenarios() -> list[es.Scenario]:
    """正例场景（非 fragile、非失败路径）：合成失败/partial outcome 必须让它们挂至少一项。"""
    return [
        s for s in _all_scenarios() if not s.fragile and s.id not in _FAILURE_PATH_SCENARIOS
    ]


def _effective_prompt(scenario: es.Scenario) -> str:
    """被评测的那一轮的 prompt（多轮场景评估最后一轮 = ``follow_up[-1]``）。"""
    return scenario.follow_up[-1] if scenario.follow_up else scenario.prompt


def _attempted_tools(scenario: es.Scenario) -> list[str]:
    """失败运行会记录的工具名 = 场景 tools_called 期望的工具（记录的是**尝试**，含失败）。

    没有 tools_called 断言的场景（P0 的数据断言）用占位工具名：那些场景的断言不看工具名，
    这里只影响合成 outcome 里 degradation 消息的形状。
    """
    tools = [
        tool
        for check in scenario.checks
        if check.type == "tools_called"
        for tool in (check.tools or [])
    ]
    return list(dict.fromkeys(tools)) or ["search"]


def _synthetic_all_failed(scenario: es.Scenario) -> dict:
    """策略链全败时的 outcome（graph 的 all_strategies_failed 形状 + answer 模板 + harness 注入）。

    忠实点：``tool_calls`` 记**尝试过**的工具；``stop_reason`` 是 "completed"（graph 只看
    ``result_data["partial"]``，全败形状没标 partial —— 所以光靠 stop_reason 抓不住全败，
    ``degradation_absent`` 才是那条断言）；错误消息回显任务原文（真实模板就是这么长）。
    """
    prompt = _effective_prompt(scenario)
    tools = _attempted_tools(scenario)
    degradation = [
        {
            "strategy": tool,
            "error": "service_error",
            "message": _TOOL_FAILURE_TEMPLATES.get(tool, _DEFAULT_FAILURE_TEMPLATE).format(
                tool=tool, prompt=prompt
            ),
        }
        for tool in tools
    ]
    return {
        "tier": scenario.tier,
        "stop_reason": "completed",
        "final_answer": (
            "抱歉，这个任务暂时无法完成："
            + "；".join(f"{d['strategy']}: {d['message']}" for d in degradation)
            + "。可以换个说法试试。"
        ),
        "result_data": {"error": "all_strategies_failed", "degradation": degradation},
        "degradation_log": degradation,
        "tool_calls": tools,
        "pending_human_action": False,
    }


def _synthetic_partial(scenario: es.Scenario) -> dict:
    """链上"部分成功"的 outcome（graph 的 partial 路径 + 模型的复述式回答）。

    ``partial`` / ``partial_reason`` 与 graph ``_observation_result`` /
    ``_navigation_only_result`` 同形；``degradation_log`` 记下那条失败的策略
    （真实 partial 一定有失败：只打开没观察 = 观察步失败）。
    """
    prompt = _effective_prompt(scenario)
    tools = _attempted_tools(scenario)
    degradation = [
        {
            "strategy": tools[-1],
            "error": "service_error",
            "message": _TOOL_FAILURE_TEMPLATES.get(
                tools[-1], _DEFAULT_FAILURE_TEMPLATE
            ).format(tool=tools[-1], prompt=prompt),
        }
    ]
    return {
        "tier": scenario.tier,
        "stop_reason": "partial",
        "final_answer": f"关于任务「{prompt}」：页面已经打开，但还没有取到内容，暂时无法给出结论。",
        "result_data": {
            "navigated": True,
            "opened_url": "https://example.com",
            "partial": True,
            "partial_reason": "no_observation_after_navigation",
        },
        "degradation_log": degradation,
        "tool_calls": tools,
        "pending_human_action": False,
    }


def _assert_not_all_green(scenario: es.Scenario, outcome: dict, kind: str) -> None:
    results = [(check, run_check(outcome, check)) for check in scenario.checks]
    detail = "; ".join(f"{check.type}={result.detail}" for check, result in results)
    assert any(not result.passed for _, result in results), (
        f"{scenario.id}: 合成{kind} outcome 让**全部**断言通过 —— 该场景在真实{kind}运行上会假通过；"
        f"明细：{detail}"
    )


@pytest.mark.parametrize("scenario", _positive_scenarios(), ids=lambda s: s.id)
def test_synthetic_all_failed_outcome_fails_at_least_one_check(scenario):
    """C1 回归：全策略失败（工具被尝试、失败文案回显任务文本）不得让正例场景全绿。"""
    _assert_not_all_green(scenario, _synthetic_all_failed(scenario), "全失败")


@pytest.mark.parametrize("scenario", _positive_scenarios(), ids=lambda s: s.id)
def test_synthetic_partial_outcome_fails_at_least_one_check(scenario):
    """C1 回归：partial（打开了/链路降级但没完成）不得让正例场景全绿。"""
    _assert_not_all_green(scenario, _synthetic_partial(scenario), "partial")


def test_synthetic_outcome_guard_covers_scenarios_and_exclusions_exist():
    """回归网的覆盖面必须显式：排除清单里的 id 必须真实存在（写错 id = 场景悄悄逃出回归网），
    且正例集合非空（空集 = 回归网什么也没盖住）。"""
    ids = {s.id for s in _all_scenarios()}
    assert _FAILURE_PATH_SCENARIOS <= ids
    assert _positive_scenarios()


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
