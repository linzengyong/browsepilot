"""P4-T3: real-mode harness —— 组合根 / 逐场景隔离 / 用量-耗时采集 / 多轮接线。

测试口径：全部替身都是**离线 fake**（fake pool / fake provider / fake executor），不启动真浏览器、
不发网络请求（计划 §执行环境：真实浏览器/模型不进 pytest）。真实模式的组合根只做"装配"验证
（``build_real_deps`` 造出的对象与 Settings 同源、池确实注入了 executor），从不 ``start()``。

三段：

- **A 生命周期**：``pool_lifecycle`` 起池 → 关池（一次）、异常路径也要关池、池起不来时
  ``attach_browser(None)`` + WARNING（诚实降级：tier3 的失败归因到环境而不是任务）。
- **B 用量/耗时**：``UsageTrackingProvider`` 只从 **chat 的 ``ChatResult.usage``** 累加
  （``structured`` 返回 schema 实例、``stream`` 按 port 只 yield 字符串 —— 两个口都没有 usage 面，
  这是 P4-T1 的探测结论）；累加器**每场景新建**（不串场景）；拿不到用量则 ``token_usage``
  **字段缺席** → ``tokens_below`` 照 FAIL（fail-loud：未知不是 0）；耗时 = 整场景墙钟。
- **C 多轮**：``follow_up`` 逐轮跑，第 n 轮拿到前几轮的 history（"Q: … / A: …" 摘要，
  与 ``app.main._build_history_entries`` 逐字一致），checks 只评估**最后一轮**的 outcome。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

import eval.harness as harness
from browsepilot.agent.tracer import JsonlTracer
from browsepilot.model.messages import ChatResult, Cost, Message, Usage, UsageInfo
from browsepilot.model.provider import ModelProvider
from eval.fake_deps import FakeExecutor, FakeProvider
from eval.run_eval import _run_scenario
from eval.scenarios import Scenario, load_scenarios

SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent / "eval" / "scenarios"


def _scenario(**overrides) -> Scenario:
    """一条最小合法场景（天气任务：规则分类 → 参数提取 → weather 工具 → answer 合成）。"""
    base: dict = {
        "id": "s-weather",
        "name": "weather",
        "prompt": "北京今天天气怎么样？",
        "checks": [{"type": "contains_keyword", "keywords": ["北京"]}],
    }
    base.update(overrides)
    return Scenario(**base)


def _tracer(tmp_path: Path, trace_id: str = "eval-harness") -> JsonlTracer:
    return JsonlTracer(trace_id=trace_id, out_dir=tmp_path)


# ── 替身：池 / executor / provider（都不碰真浏览器、不发网络） ──


class _SpyPool:
    """``BrowserPool`` 替身：只记 start/close（可注入启动失败）。"""

    def __init__(self, events: list[str], *, start_error: Exception | None = None) -> None:
        self.events = events
        self.start_error = start_error

    async def start(self) -> None:
        self.events.append("start")
        if self.start_error is not None:
            raise self.start_error

    async def close(self) -> None:
        self.events.append("close")


class _SpyExecutor(FakeExecutor):
    """记录 ``aclose()`` / ``attach_browser()`` 的 FakeExecutor（隔离与降级的观察点）。"""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def aclose(self) -> None:
        self.events.append("aclose")

    def attach_browser(self, browser) -> None:
        self.events.append(f"attach:{'pool' if browser is not None else 'none'}")


class _CloseBoomPool(_SpyPool):
    """``close()`` 炸掉的池替身（异常安全：关池失败不得掩盖 body 的异常）。"""

    async def close(self) -> None:
        self.events.append("close")
        raise RuntimeError("close exploded")


class _UsageProvider(FakeProvider):
    """FakeProvider + 每次 ``chat`` 回报**已知**用量（采集测试的输入面）。

    ``estimated=True`` 模拟"provider 没拿到 usage"（litellm ``UsageInfo(estimated=True)`` 的形状）。
    """

    def __init__(
        self,
        *,
        total_tokens: int = 100,
        input_tokens: int = 80,
        output_tokens: int = 20,
        cost_usd: float = 0.0,
        estimated: bool = False,
    ) -> None:
        self.total_tokens = total_tokens
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.estimated = estimated
        self.chats = 0

    async def chat(self, messages: list[Message], *, model: str | None = None) -> ChatResult:
        result = await super().chat(messages, model=model)
        self.chats += 1
        if self.estimated:
            return ChatResult(text=result.text, usage=UsageInfo(estimated=True))
        return ChatResult(
            text=result.text,
            usage=UsageInfo(
                usage=Usage(
                    input=self.input_tokens,
                    output=self.output_tokens,
                    total_tokens=self.total_tokens,
                ),
                cost=Cost(total=self.cost_usd),
            ),
        )


class _RecordingProvider(FakeProvider):
    """记录每次 ``chat`` 的 messages（多轮历史的证据：第二轮看得见第一轮的 Q/A）。"""

    def __init__(self) -> None:
        self.chats: list[list[Message]] = []

    async def chat(self, messages: list[Message], *, model: str | None = None) -> ChatResult:
        self.chats.append(list(messages))
        return await super().chat(messages, model=model)


class _StreamProvider(FakeProvider):
    """有流式面的 provider（FakeProvider.stream 是 NotImplementedError）。"""

    async def stream(self, messages: list[Message], *, model: str | None = None):
        for chunk in ("甲", "乙"):
            yield chunk


class _Schema(BaseModel):
    city: str = ""


class _PassThroughWatcher(FakeProvider):
    """记录 ``structured`` 收到的 model/max_retries（装饰器透传语义的证据）。"""

    def __init__(self) -> None:
        self.seen: dict = {}

    async def structured(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel],
        model: str | None = None,
        max_retries: int = 3,
    ) -> BaseModel:
        self.seen = {"model": model, "max_retries": max_retries}
        return await super().structured(
            messages, schema=schema, model=model, max_retries=max_retries
        )


# ── A. 组合根 + 池生命周期 ──


def test_build_real_deps_wires_pool_executor_and_provider():
    """组合根从**同一个** Settings 装配池/策略/executor/provider（I3）：

    池必须交到 executor 手里（不注入 = 6 个 tier3 工具全 browser_unavailable，
    那批失败会被误读成任务失败 —— P3 final review 给 P4 的关键输入）。
    """
    from browsepilot.app.browser.browser_pool import BrowserPool
    from browsepilot.app.config import Settings
    from browsepilot.app.executor import AppToolExecutor
    from browsepilot.model.litellm_provider import LiteLLMProvider

    settings = Settings()
    deps = harness.build_real_deps(settings)

    assert isinstance(deps.pool, BrowserPool)
    assert isinstance(deps.executor, AppToolExecutor)
    assert isinstance(deps.provider, LiteLLMProvider)
    assert deps.provider.model == settings.default_model
    # 池的配置与 Settings 同源（headless / max_pages / channel；空串 channel 收敛成 None）
    assert deps.pool.headless is settings.browser_headless
    assert deps.pool.max_pages == settings.browser_max_pages
    assert deps.pool.channel == (settings.browser_channel or None)
    # 注入的是**构造**注入（与 app.main/_bind_executor_browser 同一条不变式）
    assert deps.executor._browser is deps.pool


async def test_pool_lifecycle_starts_and_closes_the_pool_once():
    events: list[str] = []
    async with harness.pool_lifecycle(_SpyPool(events), _SpyExecutor(events)) as started:
        assert started is True
        assert events == ["start"]
    assert events == ["start", "close"]


async def test_pool_lifecycle_closes_the_pool_when_the_body_raises():
    """异常路径也要关池：场景炸了不能把浏览器进程留在半开状态。"""
    events: list[str] = []
    with pytest.raises(RuntimeError, match="场景炸了"):
        async with harness.pool_lifecycle(_SpyPool(events), _SpyExecutor(events)):
            raise RuntimeError("场景炸了")
    assert events == ["start", "close"]


async def test_pool_lifecycle_closes_the_pool_on_cancellation():
    """取消（``BaseException``）同样要归还池 —— 池的生命周期不看场景为什么结束。"""
    events: list[str] = []
    with pytest.raises(asyncio.CancelledError):
        async with harness.pool_lifecycle(_SpyPool(events), _SpyExecutor(events)):
            raise asyncio.CancelledError
    assert events == ["start", "close"]


async def test_pool_lifecycle_degrades_when_the_pool_cannot_start(capsys):
    """池起不来：打 WARNING + ``attach_browser(None)``，且**不假装关闭**没启动的池。

    宁可让 tier3 的失败在日志里可见地归因到环境（chromium rev 缺失），
    也不要静默产出误导性的成功率。
    """
    events: list[str] = []
    pool = _SpyPool(events, start_error=RuntimeError("chromium rev 1217 not found"))
    async with harness.pool_lifecycle(pool, _SpyExecutor(events)) as started:
        assert started is False
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "browser_unavailable" in out
    assert "chromium rev 1217 not found" in out  # 原始异常文本要留在日志里
    assert events == ["start", "attach:none"]


async def test_pool_lifecycle_close_failure_does_not_mask_the_body_exception(capsys):
    """Minor（P4-T3 审查）：关池失败不得掩盖 body 的异常 —— 诊断优先级是"场景为什么炸"，
    close 自己的问题打 WARNING（静默会把真正的原因丢掉）。"""
    events: list[str] = []
    with pytest.raises(RuntimeError, match="场景炸了"):
        async with harness.pool_lifecycle(_CloseBoomPool(events), _SpyExecutor(events)):
            raise RuntimeError("场景炸了")
    assert events == ["start", "close"]
    assert "close exploded" in capsys.readouterr().out


async def test_pool_lifecycle_propagates_close_failure_when_the_body_was_clean():
    """body 没炸时 close 的失败照常上抛：没有要保留的原异常，清理失败不该被静默吞掉。"""
    events: list[str] = []
    with pytest.raises(RuntimeError, match="close exploded"):
        async with harness.pool_lifecycle(_CloseBoomPool(events), _SpyExecutor(events)):
            pass
    assert events == ["start", "close"]


# ── B. 用量 / 耗时采集 ──


def test_usage_tracking_provider_is_a_model_provider():
    assert isinstance(harness.UsageTrackingProvider(FakeProvider()), ModelProvider)


async def test_usage_tracking_accumulates_chat_usage_and_cost():
    usage = harness.UsageAccumulator()
    provider = harness.UsageTrackingProvider(
        _UsageProvider(total_tokens=100, input_tokens=80, output_tokens=20, cost_usd=0.002),
        usage,
    )
    for _ in range(2):
        result = await provider.chat([Message(role="user", content="hi")])
        assert result.text  # 返回值原样透传
    assert usage.total_tokens == 200
    assert (usage.input_tokens, usage.output_tokens) == (160, 40)
    assert (usage.chat_calls, usage.estimated_calls) == (2, 0)
    assert usage.model_calls == 2  # 只有 chat：分母 = chat 调用数
    snapshot = usage.snapshot()
    assert snapshot is not None
    assert snapshot["total_tokens"] == 200
    assert snapshot["cost_usd"] == pytest.approx(0.004)
    assert (snapshot["input_tokens"], snapshot["output_tokens"]) == (160, 40)
    assert snapshot["usage_source"] == "chat_only"  # I2：数据来源自描述
    assert (snapshot["chat_calls"], snapshot["model_calls"]) == (2, 2)


async def test_usage_tracking_passes_structured_and_stream_through():
    """``structured`` / ``stream`` 透传（含 model / max_retries），且两边都没有 usage 面。"""
    inner = _PassThroughWatcher()
    usage = harness.UsageAccumulator()
    provider = harness.UsageTrackingProvider(inner, usage)

    result = await provider.structured(
        [Message(role="user", content="北京今天天气怎么样？")],
        schema=_Schema,
        model="small-model",
        max_retries=7,
    )
    assert isinstance(result, _Schema) and result.city == "北京"
    assert inner.seen == {"model": "small-model", "max_retries": 7}

    chunks = [chunk async for chunk in _StreamProvider().stream([])]
    assert chunks  # 替身自检：流式面确实有内容（下面验证装饰器不吞 chunk）

    chunked = [
        chunk
        async for chunk in harness.UsageTrackingProvider(_StreamProvider(), usage).stream(
            [Message(role="user", content="hi")]
        )
    ]
    assert chunked == ["甲", "乙"]
    # I2：structured/stream 没有 usage 面，但调用本身进**诚实分母**（model_calls）；
    # 没有 chat 调用 = 没有可报 token → 快照缺席（不填 0）。
    assert (usage.chat_calls, usage.structured_calls, usage.stream_calls) == (0, 1, 1)
    assert usage.model_calls == 2
    assert usage.snapshot() is None


async def test_usage_snapshot_is_none_when_the_provider_reports_no_usage():
    """未知 ≠ 0（fail-loud）：provider 没回 usage（estimated）或回全 0 时，快照必须是 ``None``。"""
    estimated = harness.UsageAccumulator()
    await harness.UsageTrackingProvider(_UsageProvider(estimated=True), estimated).chat(
        [Message(role="user", content="hi")]
    )
    assert (estimated.chat_calls, estimated.estimated_calls) == (1, 1)
    assert estimated.snapshot() is None

    # FakeProvider 的 ChatResult 用默认 UsageInfo（全 0、estimated=False）—— 调用进了分母，
    # 但没有可用 token 数：同样是"没有数字"，快照缺席（0 不是"未知"）。
    silent = harness.UsageAccumulator()
    await harness.UsageTrackingProvider(FakeProvider(), silent).chat(
        [Message(role="user", content="hi")]
    )
    assert silent.chat_calls == 1
    assert silent.snapshot() is None


async def test_usage_snapshot_omits_split_tokens_the_provider_never_reported():
    """I2：provider 只报 ``total_tokens`` 时，input/output 是**未知**而不是 0 ——
    填 0 会把"不知道"伪装成"没有输入 token"（违反模块的 fail-loud 规则）。"""
    usage = harness.UsageAccumulator()
    await harness.UsageTrackingProvider(
        _UsageProvider(total_tokens=100, input_tokens=0, output_tokens=0), usage
    ).chat([Message(role="user", content="hi")])
    snapshot = usage.snapshot()
    assert snapshot is not None
    assert snapshot["total_tokens"] == 100
    assert "input_tokens" not in snapshot and "output_tokens" not in snapshot
    assert snapshot["usage_source"] == "chat_only"


async def test_run_scenario_injects_duration_and_token_usage(tmp_path):
    """注入键名：``outcome["token_usage"]["total_tokens"]`` / ``outcome["duration_ms"]``
    （checks.py 的 ``tokens_below`` / ``latency_below`` 就是按这两个键读的）。"""
    scenario = _scenario(
        id="usage",
        checks=[
            {"type": "tools_called", "tools": ["weather"]},
            {"type": "tokens_below", "limit": 500},
            {"type": "latency_below", "limit_ms": 60000},
        ],
    )
    provider = _UsageProvider(total_tokens=100)
    outcome, checks = await _run_scenario(
        scenario, provider=provider, executor=FakeExecutor(), tracer=_tracer(tmp_path)
    )
    assert outcome["token_usage"]["total_tokens"] == 100
    assert outcome["duration_ms"] > 0
    assert outcome["tool_calls"] == ["weather"]
    assert provider.chats == 1  # 用量来自这一次 answer 合成调用
    usage = outcome["token_usage"]
    assert usage["usage_source"] == "chat_only"  # I2：只覆盖 chat 是要说清楚的
    assert usage["chat_calls"] == 1
    # 参数提取走 structured（无 usage 面）：chat 之外还有 1 次模型调用进诚实分母
    assert usage["model_calls"] == 2
    assert all(passed for passed, _ in checks), checks


async def test_run_scenario_omits_token_usage_when_it_cannot_be_captured(tmp_path):
    """采集失败 → 字段**缺席**（而不是填 0）：预算断言必须照实 FAIL，不能假通过。"""
    scenario = _scenario(id="no-usage", checks=[{"type": "tokens_below", "limit": 500}])
    outcome, checks = await _run_scenario(
        scenario, provider=FakeProvider(), executor=FakeExecutor(), tracer=_tracer(tmp_path)
    )
    assert "token_usage" not in outcome
    assert outcome["duration_ms"] > 0  # 耗时是 harness 自己量的，与 provider 无关
    assert checks[0][0] is False
    assert "no token usage" in checks[0][1]


async def test_usage_does_not_leak_across_scenarios(tmp_path):
    """累加器每场景新建：第二个场景的用量只算自己的调用（不是 200）。"""
    provider = _UsageProvider(total_tokens=100)
    tracer = _tracer(tmp_path, "eval-usage-isolation")
    first = await harness.run_scenario(
        _scenario(id="a"), provider=provider, executor=FakeExecutor(), tracer=tracer
    )
    second = await harness.run_scenario(
        _scenario(id="b"), provider=provider, executor=FakeExecutor(), tracer=tracer
    )
    assert first["token_usage"]["total_tokens"] == 100
    assert second["token_usage"]["total_tokens"] == 100
    assert first is not second


async def test_duration_ms_covers_every_turn(tmp_path):
    """duration_ms 是**整场景**（含全部轮次）的墙钟：多轮场景不能只量最后一轮
    （否则延迟/成本都被低报）。用每轮一次 chat 的睡眠累加钉住 —— 单轮口径只有一次睡眠。"""
    per_chat_s = 0.05

    class _SleepyProvider(FakeProvider):
        async def chat(self, messages, *, model=None):
            await asyncio.sleep(per_chat_s)
            return await super().chat(messages, model=model)

    scenario = _scenario(
        id="mt-duration",
        prompt="北京今天天气怎么样？",
        follow_up=["上海今天天气怎么样？"],
    )
    outcome = await harness.run_scenario(
        scenario,
        provider=_SleepyProvider(),
        executor=FakeExecutor(),
        tracer=_tracer(tmp_path, "eval-duration-multiturn"),
    )
    # 两轮各至少一次 chat → 两段睡眠；只量最后一轮时上限是一次睡眠（~50ms）
    assert outcome["duration_ms"] >= 1000 * 2 * per_chat_s * 0.9


async def test_run_scenario_teardown_wiring_runs_on_success_and_failure(tmp_path):
    """teardown（real 模式 = ``executor.aclose``）在**成功与异常**两条路径都跑：
    页面逐场景归还，场景炸了更不能把脏页留给下一个。"""
    events: list[str] = []

    async def teardown() -> None:
        events.append("teardown")

    class _ExplodingProvider(FakeProvider):
        async def chat(self, messages, *, model=None):
            raise RuntimeError("model exploded")

    await _run_scenario(
        _scenario(id="ok"),
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=_tracer(tmp_path, "eval-teardown-ok"),
        teardown=teardown,
    )
    assert events == ["teardown"]

    with pytest.raises(RuntimeError, match="model exploded"):
        await _run_scenario(
            _scenario(id="boom"),
            provider=_ExplodingProvider(),
            executor=FakeExecutor(),
            tracer=_tracer(tmp_path, "eval-teardown-boom"),
            teardown=teardown,
        )
    assert events == ["teardown", "teardown"]


async def test_run_all_executes_scenarios_through_the_harness(tmp_path, monkeypatch):
    """run_all（mock 与 real 共用的执行路径）必须走 harness —— 只有一条执行路径，
    用量/隔离/多轮才不会"实模式有、mock 没有"。"""
    from eval import run_eval

    seen: list[str] = []
    real_run_scenario = harness.run_scenario

    async def spy(scenario, **kwargs):
        seen.append(scenario.id)
        return await real_run_scenario(scenario, **kwargs)

    monkeypatch.setattr(harness, "run_scenario", spy)
    code = await run_eval.run_all(
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=_tracer(tmp_path, "eval-wiring"),
        mode="mock",
        tags=["smoke"],
    )
    assert code == 0
    assert seen  # 每条跑过的场景都经过 harness.run_scenario


# ── C. 多轮接线（I4：multi_turn_context 的所有者） ──


async def test_multi_turn_injects_history_into_the_follow_up_turn(tmp_path):
    """第二轮的模型调用里必须出现第一轮的 "Q: … / A: …" 摘要（工具数据路径）。"""
    scenario = _scenario(
        id="mt-weather",
        prompt="北京今天天气怎么样？",
        follow_up=["上海今天天气怎么样？"],
    )
    provider = _RecordingProvider()
    outcome = await harness.run_scenario(
        scenario, provider=provider, executor=FakeExecutor(), tracer=_tracer(tmp_path)
    )
    assert len(provider.chats) == 2  # 每轮一次 answer 合成
    first_turn = "\n".join(m.content for m in provider.chats[0])
    second_turn = "\n".join(m.content for m in provider.chats[1])
    assert "历史对话：\n（无历史）" in first_turn  # 首轮没有历史
    assert "Q: 北京今天天气怎么样？" in second_turn
    assert "A: 北京今天晴，气温 26.0°C。" in second_turn
    assert outcome["final_answer"].startswith("上海")  # 返回的是最后一轮的 outcome


async def test_multi_turn_injects_history_into_a_direct_answer_turn(tmp_path):
    """直答路径（chitchat/knowledge_qa）同样带历史 —— graph 的两条 answer 分支都吃 history。"""
    scenario = _scenario(
        id="mt-chitchat",
        prompt="北京今天天气怎么样？",
        follow_up=["你好"],
    )
    provider = _RecordingProvider()
    await harness.run_scenario(
        scenario, provider=provider, executor=FakeExecutor(), tracer=_tracer(tmp_path)
    )
    second_turn = "\n".join(m.content for m in provider.chats[1])
    assert "Q: 北京今天天气怎么样？" in second_turn
    assert "任务：你好" in second_turn


async def test_multi_turn_checks_evaluate_the_last_turn(tmp_path):
    """checks 只看**最后一轮**：第一轮的实体（北京）不在最后一轮答案里 —— 断言不得被它满足。"""
    scenario = _scenario(
        id="mt-last-turn",
        prompt="北京今天天气怎么样？",
        follow_up=["上海今天天气怎么样？"],
        checks=[
            {"type": "contains_keyword", "keywords": ["上海"]},
            {"type": "contains_keyword", "keywords": ["北京"]},
        ],
    )
    outcome, checks = await _run_scenario(
        scenario, provider=FakeProvider(), executor=FakeExecutor(), tracer=_tracer(tmp_path)
    )
    assert checks[0][0] is True, checks[0]
    assert checks[1][0] is False, checks[1]
    assert "上海" in outcome["final_answer"]


async def test_multi_turn_records_every_turns_tool_calls(tmp_path):
    """tool_calls 是**整场景**的口径（各轮累加）：多轮场景的工具链要看全，不能只剩最后一轮。"""
    scenario = _scenario(
        id="mt-tools",
        prompt="北京今天天气怎么样？",
        follow_up=["上海今天天气怎么样？"],
    )
    outcome = await harness.run_scenario(
        scenario, provider=FakeProvider(), executor=FakeExecutor(), tracer=_tracer(tmp_path)
    )
    assert outcome["tool_calls"] == ["weather", "weather"]


def test_history_entries_match_the_app_formatter():
    """history 的格式必须与 app 逐字一致（``Q: <task> / A: <answer>``、压单行、截断 200、
    只留最近 5 条）—— 否则"真实模式的多轮"与生产的多轮不是一回事。"""
    from browsepilot.app.main import _build_history_entries

    turns = [
        {"task": "北京今天天气怎么样？", "answer": "晴，26°C"},
        {"task": "多行\n任务", "answer": "多行\n回答"},
        {"task": "长" * 300, "answer": "答" * 300},
    ]
    assert harness.history_entries(turns) == _build_history_entries(turns)
    assert len(harness.history_entries(turns)[-1]) == 200  # 截断上限
    capped = harness.history_entries([{"task": f"q{i}", "answer": "a"} for i in range(7)])
    assert capped == _build_history_entries([{"task": f"q{i}", "answer": "a"} for i in range(7)])
    assert len(capped) == 5  # 只留最近 5 条
    assert capped[0].startswith("Q: q2")


async def test_scenario_timeout_is_enforced_per_turn(tmp_path):
    class _SlowProvider(FakeProvider):
        async def chat(self, messages, *, model=None):
            await asyncio.sleep(0.2)
            return await super().chat(messages, model=model)

    scenario = _scenario(id="slow", timeout_seconds=0.01)
    with pytest.raises(TimeoutError) as excinfo:
        await harness.run_scenario(
            scenario,
            provider=_SlowProvider(),
            executor=FakeExecutor(),
            tracer=_tracer(tmp_path),
        )
    assert "timeout_seconds=0.01" in str(excinfo.value)
    assert "slow" in str(excinfo.value)


async def test_inner_timeout_is_not_mislabelled_as_a_scenario_timeout(tmp_path):
    """Minor（P4-T3 审查）：内层自己的 ``TimeoutError``（socket/HTTP 超时）必须原样上抛 ——
    包装成"场景超时"会把诊断指向错误的预算（调 ``timeout_seconds`` 治不好 DNS 超时）。"""

    class _InnerTimeoutProvider(FakeProvider):
        async def chat(self, messages, *, model=None):
            raise TimeoutError("socket timed out")

    scenario = _scenario(id="inner-timeout", timeout_seconds=5)
    with pytest.raises(TimeoutError) as excinfo:
        await harness.run_scenario(
            scenario,
            provider=_InnerTimeoutProvider(),
            executor=FakeExecutor(),
            tracer=_tracer(tmp_path),
        )
    assert str(excinfo.value) == "socket timed out"  # 原样：不是场景超时文案
    assert excinfo.value.__cause__ is None
    assert "超过 timeout_seconds" not in str(excinfo.value)


def test_multi_turn_context_scenario_is_enabled_and_real_only():
    """I4：这条场景现在有主了 —— 启用、real-only（mock 的 fake 在"那明天呢？"上走不到带历史的
    answer 路径，所以 mock 下必须 SKIP，而不是假装跑过）。"""
    scenario = next(s for s in load_scenarios(SCENARIOS_DIR) if s.id == "multi_turn_context")
    assert scenario.enabled is True
    assert scenario.mode == "real"
    assert scenario.follow_up == ["那明天呢？"]


def test_multi_turn_check_can_only_pass_through_history():
    """防假通过（T2-fix 红线）：断言的实体只在**第一轮** prompt 里出现，
    最后一轮 prompt（被实际执行、被评估的那轮）里没有 —— 它若通过，只可能是历史注入生效。"""
    scenario = next(s for s in load_scenarios(SCENARIOS_DIR) if s.id == "multi_turn_context")
    last_prompt = scenario.follow_up[-1]
    assert scenario.checks, "场景必须至少有一条断言"
    for check in scenario.checks:
        assert check.type == "contains_keyword"
        for keyword in check.keywords or []:
            assert keyword in scenario.prompt, keyword
            assert keyword not in last_prompt, keyword
