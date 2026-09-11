import json

import pytest

from browsepilot.agent.graph import _answer_payload, _merge_degradation, build_graph
from browsepilot.agent.params import WeatherParams
from browsepilot.agent.router import IntentResult
from browsepilot.agent.state import AgentState
from browsepilot.agent.tools import ToolResult, ToolSpec
from browsepilot.agent.tracer import JsonlTracer
from browsepilot.model.messages import ChatResult
from browsepilot.model.structured import StructuredOutputError


class StubExecutor:
    async def list_tools(self):
        return [ToolSpec(name="echo", description="echo back")]

    async def call(self, name, arguments):
        if name == "weather":
            return ToolResult(
                status="success",
                content="北京：晴，26.0°C",
                data={"city": "北京", "temperature_c": 26.0},
            )
        return ToolResult(content="stub")


class HumanNeedingExecutor(StubExecutor):
    """Executor whose weather tool needs human confirmation before answering."""

    async def call(self, name, arguments):
        if name == "weather":
            return ToolResult(
                status="success",
                data={"needs_human": True, "human_prompt": "请确认城市"},
            )
        return await super().call(name, arguments)


class FailingExecutor:
    async def list_tools(self):
        return [ToolSpec(name="echo", description="echo back")]

    async def call(self, name, arguments):
        return ToolResult(status="error", error_type="service_error", error_message="boom")


class StubProvider:
    """Model provider stub: structured classifies weather; chat echoes task."""

    def __init__(self):
        self.structured_calls = 0
        self.last_user_content = ""

    async def chat(self, messages, *, model=None):
        self.last_user_content = messages[-1].content
        return ChatResult(text=f"answer for {messages[-1].content[:20]}")

    async def stream(self, messages, *, model=None):
        raise NotImplementedError

    async def structured(self, messages, *, schema, model=None, max_retries=3):
        self.structured_calls += 1
        if schema is IntentResult:
            return IntentResult(intent="weather", tier="tier1")
        return schema(city="北京", query="北京天气", url="https://example.com")


class FlakyExecutor(StubExecutor):
    """weather 城市未命中失败一次，search 兜底成功。"""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail_weather = True

    async def call(self, name, arguments):
        self.calls.append(name)
        if name == "weather" and self.fail_weather:
            return ToolResult(status="error", error_type="city_not_found", error_message="无此城市")
        if name == "search":
            return ToolResult(status="success", data={"results": []}, content="搜索结果")
        return await super().call(name, arguments)


class SearchFailingExecutor(StubExecutor):
    """search 工具失败（search 意图只有单一策略，失败即最终降级）。"""

    async def call(self, name, arguments):
        if name == "search":
            return ToolResult(status="error", error_type="search_timeout", error_message="搜索超时")
        return await super().call(name, arguments)


class RecordingExecutor(StubExecutor):
    """记录每次工具调用的参数，search 返回空结果页。"""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "search":
            return ToolResult(status="success", data={"results": []}, content="无结果")
        return await super().call(name, arguments)


class ParamFailingProvider(StubProvider):
    """天气参数提取失败，其余 schema 正常。"""

    async def structured(self, messages, *, schema, model=None, max_retries=3):
        if schema is WeatherParams:
            raise StructuredOutputError("无法解析城市参数")
        return await super().structured(
            messages, schema=schema, model=model, max_retries=max_retries
        )


@pytest.mark.asyncio
async def test_skeleton_graph_runs_and_traces(tmp_path):
    tracer = JsonlTracer(trace_id="test-trace", out_dir=tmp_path / "traces")
    provider = StubProvider()
    graph = build_graph(StubExecutor(), tracer, provider)
    result = await graph.ainvoke(AgentState(task="查天气"))
    # LangGraph 1.x ainvoke returns a dict (see plan T9 note)
    assert result["final_answer"] != ""
    assert result["stop_reason"] == "completed"
    assert result["result_data"]["temperature_c"] == 26.0
    # "查天气" hits the weather rule -> rules-first classify, no model call
    # there; execute calls structured once for param extraction
    assert result["intent"] == "weather"
    assert result["tier"] == "tier1"
    assert provider.structured_calls == 1
    trace_path = tmp_path / "traces" / "test-trace.jsonl"
    lines = trace_path.read_text(encoding="utf-8").strip().splitlines()
    # classify start/end + decision + execute start/end + tool_call + answer start/end
    assert len(lines) == 8
    events = [json.loads(line) for line in lines]
    decisions = [e for e in events if e["event_type"] == "decision"]
    assert len(decisions) == 1
    assert decisions[0]["node"] == "classify"
    assert decisions[0]["data"]["intent"] == "weather"
    assert decisions[0]["data"]["tier"] == "tier1"
    assert decisions[0]["data"]["confidence"] == 1.0
    tool_calls = [e for e in events if e["event_type"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["node"] == "execute"
    assert tool_calls[0]["data"]["tool"] == "weather"
    assert tool_calls[0]["data"]["status"] == "success"


@pytest.mark.asyncio
async def test_graph_falls_back_to_model_when_rules_miss(tmp_path):
    tracer = JsonlTracer(trace_id="test-trace-fallback", out_dir=tmp_path / "traces")
    provider = StubProvider()
    graph = build_graph(StubExecutor(), tracer, provider)
    result = await graph.ainvoke(AgentState(task="帮我处理一下那个事情"))
    # rules miss -> small model classifies (stub returns weather/tier1);
    # execute then calls structured again for param extraction
    assert result["intent"] == "weather"
    assert result["tier"] == "tier1"
    assert provider.structured_calls == 2


@pytest.mark.asyncio
async def test_weather_task_runs_tool_and_synthesizes(tmp_path):
    tracer = JsonlTracer(trace_id="t2", out_dir=tmp_path / "traces")
    graph = build_graph(StubExecutor(), tracer, StubProvider())
    result = await graph.ainvoke(AgentState(task="北京今天天气怎么样？"))
    assert result["intent"] == "weather"
    assert result["result_data"]["temperature_c"] == 26.0
    assert result["final_answer"] != ""
    assert result["stop_reason"] == "completed"


@pytest.mark.asyncio
async def test_chitchat_routes_straight_to_answer_without_tool_call(tmp_path):
    tracer = JsonlTracer(trace_id="t3", out_dir=tmp_path / "traces")
    provider = StubProvider()
    graph = build_graph(StubExecutor(), tracer, provider)
    result = await graph.ainvoke(AgentState(task="你好"))
    assert result["intent"] == "chitchat"
    assert result["tier"] == "answer"
    assert result["final_answer"] == "answer for 你好"
    assert result["result_data"] == {}
    assert provider.structured_calls == 0
    trace_path = tmp_path / "traces" / "t3.jsonl"
    lines = trace_path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    assert not [e for e in events if e["event_type"] == "tool_call"]


@pytest.mark.asyncio
async def test_answer_uses_history_context(tmp_path):
    tracer = JsonlTracer(trace_id="t4", out_dir=tmp_path / "traces")
    provider = StubProvider()
    graph = build_graph(StubExecutor(), tracer, provider)
    result = await graph.ainvoke(
        AgentState(task="你好", history=["Q: 北京天气 / A: 晴 25°C"])
    )
    assert result["final_answer"] != ""
    # answer 节点必须把历史摘要注入给模型的最后一条 user 消息
    assert "北京天气" in provider.last_user_content


@pytest.mark.asyncio
async def test_graph_with_sqlite_checkpointer_persists_state(tmp_path):
    """Checkpointed run persists final state so aget_state can resume it.

    Installed langgraph-checkpoint-sqlite (3.x) API: from_conn_string is an
    async context manager that closes the underlying aiosqlite connection on
    exit; setup() is idempotent and safe to call explicitly.
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "ckpt.sqlite")) as saver:
        await saver.setup()
        tracer = JsonlTracer(trace_id="t3", out_dir=tmp_path / "traces")
        graph = build_graph(StubExecutor(), tracer, StubProvider(), checkpointer=saver)
        config = {"configurable": {"thread_id": "session-1"}}
        await graph.ainvoke(
            AgentState(task="北京今天天气怎么样？", session_id="session-1"), config=config
        )
        state_snapshot = await graph.aget_state(config)
        assert state_snapshot.values["intent"] == "weather"
        assert state_snapshot.values["stop_reason"] == "completed"


@pytest.mark.asyncio
async def test_hitl_interrupt_then_resume(tmp_path):
    """HITL 骨架：execute 返回 needs_human → hitl 节点 interrupt 暂停；
    Command(resume=…) 继续并把 human_response 带回最终状态。

    LangGraph 1.1.x 实测：ainvoke 不抛 GraphInterrupt，而是直接返回
    带 __interrupt__ 的暂停状态（checkpoint 里 next=('hitl',)）。
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    tracer = JsonlTracer(trace_id="t5", out_dir=tmp_path / "traces")
    graph = build_graph(
        HumanNeedingExecutor(), tracer, StubProvider(), checkpointer=MemorySaver()
    )
    config = {"configurable": {"thread_id": "s1"}}
    result = await graph.ainvoke(AgentState(task="北京天气"), config=config)
    assert result.get("__interrupt__")  # interrupt 已触发，运行暂停
    assert result["pending_human_action"] is True
    assert result["human_prompt"] == "请确认城市"
    assert result["final_answer"] == ""  # answer 节点尚未执行
    assert result["degradation_log"] == []  # 链上首次尝试即命中 needs_human，无降级
    snap = await graph.aget_state(config)
    assert snap.next == ("hitl",)  # 暂停在 hitl 节点，等待 resume
    assert snap.values["pending_human_action"] is True
    resumed = await graph.ainvoke(Command(resume="确认，就北京"), config=config)
    assert resumed["human_response"] == "确认，就北京"
    assert resumed["pending_human_action"] is False
    assert resumed["hitl_triggered_count"] == 1
    # 负对照（F3 的反面）：暂停在**非导航步**（weather 的结果本身就是任务数据）→ 仍是 completed
    assert resumed["stop_reason"] == "completed"


# ── C1：HITL resume 闭环（人工反馈必须回到答案的上下文里） ───────────────────


class LoginWallExecutor(StubExecutor):
    """browser_open 命中登录墙：needs_human + 真实文案（D5/D6 桥的产出形状）。"""

    def __init__(self, url: str = "https://example.com/login") -> None:
        super().__init__()
        self.calls: list[str] = []
        self.url = url

    async def call(self, name, arguments):
        self.calls.append(name)
        if name == "browser_snapshot":  # 新任务：还没有打开的页面（真实的首次调用）
            return ToolResult(
                status="error",
                error_type="no_open_page",
                error_message="还没有打开的页面",
            )
        if name == "browser_open":
            return ToolResult(
                status="success",
                content=f"已打开 {self.url}",
                data={
                    "action": "browser_open",
                    "url": self.url,
                    "needs_human": True,
                    "human_prompt": "页面需要登录：请在浏览器中手动登录后继续任务。",
                    "intervention": "login",
                },
            )
        return await super().call(name, arguments)


class LoginHookedProvider(StubProvider):
    """会被"过期提示"带偏的模型替身：看到 human_prompt 就复述"请手动登录"。

    真实模型在 P3 final review C1 里就是这个形状：answer 只把 ``result_data``
    （里面还挂着 ``human_prompt``）喂给它，人工反馈根本没进上下文。
    """

    def __init__(self) -> None:
        super().__init__()
        self.last_system_content = ""

    async def chat(self, messages, *, model=None):
        self.last_user_content = messages[-1].content
        first = messages[0] if messages else None
        self.last_system_content = first.content if first is not None and first.role == "system" else ""
        if "人工反馈" in self.last_user_content:
            return ChatResult(text="已基于人工反馈继续：登录后的页面已经打开。")
        if "human_prompt" in self.last_user_content:
            return ChatResult(text="请在浏览器中手动登录后继续任务。")
        return ChatResult(text="（没有可用上下文）")


@pytest.mark.asyncio
async def test_hitl_resume_feeds_human_response_back_to_the_answer(tmp_path):
    """C1：resume 后 answer 必须把**人工反馈**喂进上下文，并丢掉已过期的"请人工去做 X"提示。

    P3 final review C1 实证：``human_response`` 是写-only 字段（hitl 节点写、全仓无读取方），
    而 ``result_data`` 里仍带着 ``human_prompt="…请手动登录…"`` → resume 后的答案大概率
    复述"请手动登录"，人工确认等于白做（HITL 全链路死胡同）。
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    tracer = JsonlTracer(trace_id="t16", out_dir=tmp_path / "traces")
    ex = LoginWallExecutor()
    provider = LoginHookedProvider()
    graph = build_graph(ex, tracer, provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "c1-hitl"}}

    # 任务文本里刻意不带 URL：带 URL 的任务按既有规则走 read_page（tier2），
    # 而登录墙是 tier3 的 browser_open 走出来的（URL 由模型从任务里提取）
    paused = await graph.ainvoke(AgentState(task="帮我点击页面上的登录按钮"), config=config)
    assert paused["intent"] == "browser_task"
    assert paused["pending_human_action"] is True
    assert "browser_open" in ex.calls
    assert paused["final_answer"] == ""  # 暂停在 hitl 节点

    resumed = await graph.ainvoke(
        Command(resume="我已经在浏览器里登录好了，现在停在账户页"), config=config
    )
    assert resumed["human_response"] == "我已经在浏览器里登录好了，现在停在账户页"
    assert [name for name in ex.calls].count("browser_open") == 1  # resume 不重放工具

    # ① 答案模型看到的 user prompt：人工反馈在里面，过期提示不在（数据里也不带）
    prompt = provider.last_user_content
    assert "我已经在浏览器里登录好了，现在停在账户页" in prompt
    assert "人工反馈" in prompt
    assert "手动登录" not in prompt  # 过期的 human_prompt 已从 data 里剔除
    assert "needs_human" not in prompt  # F4：引发暂停的控制面字段也不进上下文
    assert "intervention" not in prompt
    # system prompt 里把"人工已经做完"说清楚（否则模型会再请人工操作一遍）
    assert "不要再让用户手动登录" in provider.last_system_content

    # ② 最终答案：由人工反馈驱动，不再要求人工
    assert "已基于人工反馈继续" in resumed["final_answer"]
    assert "手动登录" not in resumed["final_answer"]
    # ③ F3：暂停点在**导航步**（browser_open）上 → resume 后没有观察，不得报 completed
    #（同一场景的详细断言见 test_hitl_pause_on_the_navigation_step_resumes_as_partial）
    assert resumed["stop_reason"] == "partial"
    assert resumed["result_data"]["partial_reason"] == "hitl_no_observation"


# ── F4：answer payload 里的 HITL 控制面字段 ──────────────────────────────────


def test_answer_payload_drops_the_hitl_control_fields():
    """F4：``_answer_payload`` 要剥掉引发暂停的**控制面**字段（``needs_human`` / ``intervention``）。

    C1 只剥了 ``human_prompt``：``needs_human=True`` 还在数据里，resume 后唯一防线是 system
    里那句“人工已经做完”——模型完全可以顺着数据里的“需要人工”再要求人工确认一遍。
    ``intervention``（human_prompt 的机器可读副本）是同一类过期信息，一并剥掉。
    """
    state = AgentState(
        result_data={
            "action": "browser_open",
            "url": "https://example.com/login",
            "needs_human": True,
            "human_prompt": "页面需要登录：请在浏览器中手动登录后继续任务。",
            "intervention": "login",
        },
        human_response="我已经在浏览器里登录好了",
    )
    data = _answer_payload(state)
    assert data["human_response"] == "我已经在浏览器里登录好了"
    assert "needs_human" not in data
    assert "human_prompt" not in data
    assert "intervention" not in data
    assert data["url"] == "https://example.com/login"  # 数据面照常保留


def test_answer_payload_without_a_resume_keeps_the_prompt_but_drops_the_flag():
    """F4 的边界：没 resume 就进 answer 的路径（hitl 预算用完，第二次 needs_human 不再暂停）。

    那时 ``human_prompt`` / ``intervention`` 是模型如实告知“需要人工”的唯一提示，保留；
    而 ``needs_human`` 是“转 hitl 暂停”的控制面开关，从不进上下文。
    """
    state = AgentState(
        result_data={
            "action": "browser_open",
            "url": "https://example.com/login",
            "needs_human": True,
            "human_prompt": "请手动登录",
            "intervention": "login",
        }
    )
    data = _answer_payload(state)
    assert "needs_human" not in data
    assert data["human_prompt"] == "请手动登录"
    assert data["intervention"] == "login"


# ── F3：暂停在导航步上的 HITL，resume 后不得报 completed ─────────────────────


@pytest.mark.asyncio
async def test_hitl_pause_on_the_navigation_step_resumes_as_partial(tmp_path):
    """F3：暂停点是**导航步**（browser_open）时，resume 后没有任何观察 —— 不得报 completed。

    链是 [browser_open, browser_snapshot]：open 命中登录墙 → HITL 暂停 →（人工做完）→ answer。
    resume 走 hitl → answer，观察步**从未运行**：``result_data`` 只有 open 元数据（+needs_human），
    模型可能对从未读过的页面作答 —— 旧行为把这种结果报成 ``completed``（假成功）。
    标 ``partial`` + 稳定 reason（与 no_navigation_for_task / no_observation_after_navigation 同族）。
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    tracer = JsonlTracer(trace_id="t19", out_dir=tmp_path / "traces")
    ex = LoginWallExecutor()
    provider = StubProvider()
    graph = build_graph(ex, tracer, provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "f3-hitl-nav"}}

    paused = await graph.ainvoke(AgentState(task="帮我点击页面上的登录按钮"), config=config)
    assert paused["pending_human_action"] is True
    assert paused["final_answer"] == ""  # 暂停在 hitl 节点

    resumed = await graph.ainvoke(Command(resume="我已经登录好了，现在停在账户页"), config=config)

    assert resumed["stop_reason"] == "partial"  # ← 不是 completed
    assert resumed["result_data"]["partial"] is True
    assert resumed["result_data"]["partial_reason"] == "hitl_no_observation"
    # 观察步确实从未运行（链上只有导航那一次调用）
    assert "browser_snapshot" not in ex.calls
    # 答案模型被告知这次是 partial：不能把“只打开了页面”说成完成
    assert "partial" in provider.last_user_content
    assert "没有完成与任务目标匹配" in provider.last_user_content


@pytest.mark.asyncio
async def test_tool_failure_degrades_with_explicit_reason(tmp_path):
    """全策略失败：result_data 报 all_strategies_failed，degradation 逐次可追溯。"""
    tracer = JsonlTracer(trace_id="t4", out_dir=tmp_path / "traces")
    graph = build_graph(FailingExecutor(), tracer, StubProvider())
    result = await graph.ainvoke(AgentState(task="北京天气怎么样？"))
    assert result["intent"] == "weather"
    assert result["result_data"]["error"] == "all_strategies_failed"
    degradation = result["result_data"]["degradation"]
    assert [d["strategy"] for d in degradation] == ["weather", "search"]
    assert all(d["error"] == "service_error" for d in degradation)
    assert all(d["message"] == "boom" for d in degradation)
    assert result["degradation_log"] == degradation
    assert "无法完成" in result["final_answer"]
    assert "boom" in result["final_answer"]


@pytest.mark.asyncio
async def test_weather_falls_back_to_search(tmp_path):
    """天气城市未命中 → search 用 task 原文兜底；每次尝试都记 degradation + trace。"""
    tracer = JsonlTracer(trace_id="t6", out_dir=tmp_path / "traces")
    ex = FlakyExecutor()
    graph = build_graph(ex, tracer, StubProvider())
    result = await graph.ainvoke(AgentState(task="北京天气"))
    assert ex.calls == ["weather", "search"]
    assert result["degradation_log"][0]["strategy"] == "weather"
    assert result["degradation_log"][0]["error"] == "city_not_found"
    assert result["result_data"]["results"] == []
    # 两次尝试都有 tool_call 跟踪（status 一次 error 一次 success）
    lines = (tmp_path / "traces" / "t6.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    tool_calls = [e for e in events if e["event_type"] == "tool_call"]
    assert [(e["data"]["tool"], e["data"]["status"]) for e in tool_calls] == [
        ("weather", "error"),
        ("search", "success"),
    ]


@pytest.mark.asyncio
async def test_param_extraction_failure_falls_back_to_task_text_search(tmp_path):
    """参数提取失败 → degradation 记录 param_extraction_failed；
    search 兜底不再结构化提取，直接用 task 原文作 query。"""
    tracer = JsonlTracer(trace_id="t7", out_dir=tmp_path / "traces")
    ex = RecordingExecutor()
    graph = build_graph(ex, tracer, ParamFailingProvider())
    result = await graph.ainvoke(AgentState(task="北京天气"))
    assert [name for name, _ in ex.calls] == ["search"]
    assert ex.calls[0][1] == {"query": "北京天气"}  # 原文兜底，不重新结构化
    assert result["degradation_log"] == [
        {"strategy": "weather", "error": "param_extraction_failed", "message": "无法解析城市参数"}
    ]
    assert result["result_data"]["results"] == []


@pytest.mark.asyncio
async def test_search_intent_single_strategy_failure_is_final(tmp_path):
    """search 意图无回退链：失败即 all_strategies_failed，degradation 只有一条。"""
    tracer = JsonlTracer(trace_id="t8", out_dir=tmp_path / "traces")
    graph = build_graph(SearchFailingExecutor(), tracer, StubProvider())
    result = await graph.ainvoke(AgentState(task="搜索 Python 教程"))
    assert result["intent"] == "search"
    assert result["result_data"]["error"] == "all_strategies_failed"
    assert [d["strategy"] for d in result["result_data"]["degradation"]] == ["search"]
    assert [d["strategy"] for d in result["degradation_log"]] == ["search"]
    assert "搜索超时" in result["final_answer"]


def test_merge_degradation_dedupes_same_strategy_and_error():
    """同一 (strategy, error) 只保留首次记录 —— resume 重放不翻倍。"""
    existing = [{"strategy": "weather", "error": "city_not_found", "message": "无此城市"}]
    new = [
        {"strategy": "weather", "error": "city_not_found", "message": "无此城市"},
        {"strategy": "search", "error": "service_error", "message": "boom"},
    ]
    merged = _merge_degradation(existing, new)
    assert merged == [
        {"strategy": "weather", "error": "city_not_found", "message": "无此城市"},
        {"strategy": "search", "error": "service_error", "message": "boom"},
    ]
    # 不改动入参：调用方（state / 本轮 degradation）继续独立可用
    assert existing == [{"strategy": "weather", "error": "city_not_found", "message": "无此城市"}]
    assert new == [
        {"strategy": "weather", "error": "city_not_found", "message": "无此城市"},
        {"strategy": "search", "error": "service_error", "message": "boom"},
    ]


def test_merge_degradation_keeps_distinct_errors_on_same_strategy():
    """去重键是 (strategy, error)：同策略的不同错误类型都必须保留。"""
    existing = [{"strategy": "search", "error": "search_timeout", "message": "搜索超时"}]
    new = [{"strategy": "search", "error": "service_error", "message": "boom"}]
    assert _merge_degradation(existing, new) == existing + new


@pytest.mark.asyncio
async def test_degradation_log_not_duplicated_when_execute_replays(tmp_path):
    """resume 重放防护：execute 带着已持久化的 degradation_log 重跑时不再翻倍。

    P2 review 场景：LangGraph resume 重放 interrupt 前的 execute，本轮
    degradation 追加到 state 里已存在的 degradation_log → 同一条记录翻倍。
    这里用"第一次运行的终态"作为重放输入（即 execute 重跑时读到的 state），
    断言合并后 weather/search 仍各只有一条。
    """
    tracer = JsonlTracer(trace_id="t9", out_dir=tmp_path / "traces")
    graph = build_graph(FailingExecutor(), tracer, StubProvider())
    first = await graph.ainvoke(AgentState(task="北京天气怎么样？"))
    assert [d["strategy"] for d in first["degradation_log"]] == ["weather", "search"]

    replay_input = AgentState(
        task="北京天气怎么样？",
        degradation_log=[dict(d) for d in first["degradation_log"]],
    )
    replayed = await graph.ainvoke(replay_input)
    assert [d["strategy"] for d in replayed["degradation_log"]] == ["weather", "search"]
    assert replayed["degradation_log"] == first["degradation_log"]


# ── Tier3（browser_task / read_page 升级）策略链 ───────────────────────────────


class BrowserExecutor(StubExecutor):
    """browser_task 链的替身：默认“没有打开的页面”（open 得先建页，snapshot 才能看）。

    可编程缝：``page_open``（是否已有上一次运行留下的页面）、``open_error`` /
    ``snapshot_error``（对应工具的失败 error_type，空串 = 成功）、``browser_error``
    （整个 Tier3 不可用：每个浏览器工具都降级）。
    """

    def __init__(
        self,
        *,
        page_open: bool = False,
        browser_error: str = "",
        open_error: str = "",
        snapshot_error: str = "",
    ) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self.page_open = page_open
        self.browser_error = browser_error
        self.open_error = open_error
        self.snapshot_error = snapshot_error

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name.startswith("browser_"):
            if self.browser_error:  # 模拟真实的"未注入 BrowserPool"：每个浏览器工具都降级
                return ToolResult(
                    status="error",
                    error_type=self.browser_error,
                    error_message=f"浏览器工具失败：{self.browser_error}",
                )
            if name == "browser_open":
                if self.open_error:
                    return ToolResult(
                        status="error",
                        error_type=self.open_error,
                        error_message=f"导航失败：{self.open_error}",
                    )
                self.page_open = True
                url = arguments.get("url", "")
                return ToolResult(
                    status="success",
                    content=f"已打开 {url}",
                    data={"action": "browser_open", "url": url, "requested_url": url},
                )
            if name == "browser_snapshot":
                if self.snapshot_error:
                    return ToolResult(
                        status="error",
                        error_type=self.snapshot_error,
                        error_message=f"快照失败：{self.snapshot_error}",
                    )
                if not self.page_open:
                    return ToolResult(
                        status="error",
                        error_type="no_open_page",
                        error_message="还没有打开任何页面",
                    )
                refs = '- button "搜索" [ref=e2]'
                return ToolResult(
                    status="success",
                    content=refs,
                    data={"action": "snapshot", "url": "https://example.com/", "refs": refs},
                )
        return await super().call(name, arguments)


class FetchFailingExecutor(StubExecutor):
    """tier2 → tier3 升级链的替身：fetch_page 失败、browser_open 兜底。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "fetch_page":
            return ToolResult(status="error", error_type="fetch_failed", error_message="连接超时")
        if name == "browser_open":
            url = arguments.get("url", "")
            return ToolResult(status="success", content=f"已打开 {url}", data={"url": url})
        return await super().call(name, arguments)


@pytest.mark.asyncio
async def test_browser_task_navigates_then_observes_without_fake_degradation(tmp_path):
    """C2a：tier3 链改成**先导航后观察** —— 新任务不再先造一次假的 no_open_page 降级。

    P3 final review C2a：旧链 [snapshot, open] 让每个新任务（还没有当前页）都先产出一条
    ``no_open_page`` 降级记录 —— 任务其实成功了，P4 却会把每个 tier3 任务算一次失败。
    导航成功也不等于任务完成：任务目标在页面上，所以链继续走观察步（snapshot）。
    """
    tracer = JsonlTracer(trace_id="t10", out_dir=tmp_path / "traces")
    ex = BrowserExecutor(page_open=False)
    provider = StubProvider()
    graph = build_graph(ex, tracer, provider)
    result = await graph.ainvoke(AgentState(task="帮我比价 iPhone 15 和 小米 15"))
    assert result["intent"] == "browser_task"
    assert result["tier"] == "tier3"
    assert [name for name, _ in ex.calls] == ["browser_open", "browser_snapshot"]
    assert ex.calls[0][1]["url"] == "https://example.com"  # 导航参数来自模型提取
    assert ex.calls[1][1] == {}  # snapshot 无参数：不需要模型提取
    # ← C2a 核心：没有假降级（旧行为固定产出一条 no_open_page）
    assert result["degradation_log"] == []
    assert result["result_data"]["refs"].startswith("- button")
    assert result["result_data"]["navigated"] is True
    assert result["result_data"]["opened_url"] == "https://example.com"
    assert "partial" not in result["result_data"]
    assert result["stop_reason"] == "completed"
    # 两步都有 tool_call 跟踪（都成功）
    lines = (tmp_path / "traces" / "t10.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    tool_calls = [e for e in events if e["event_type"] == "tool_call"]
    assert [(e["data"]["tool"], e["data"]["status"]) for e in tool_calls] == [
        ("browser_open", "success"),
        ("browser_snapshot", "success"),
    ]


@pytest.mark.asyncio
async def test_browser_task_navigates_even_when_a_page_is_already_open(tmp_path):
    """C2b：当前页可能是上一次运行/别的会话留下的 —— 拿它的快照答本任务 = 假成功 + 答非所问。

    所以链不再"有页面就停在 snapshot"：任务里有 URL 就导航过去，快照拍的是任务目标页面。
    """
    tracer = JsonlTracer(trace_id="t11", out_dir=tmp_path / "traces")
    ex = BrowserExecutor(page_open=True)
    provider = StubProvider()
    graph = build_graph(ex, tracer, provider)
    result = await graph.ainvoke(AgentState(task="帮我点击登录按钮"))
    assert [name for name, _ in ex.calls] == ["browser_open", "browser_snapshot"]
    assert ex.calls[0][1]["url"] == "https://example.com"  # 重新导航到任务目标
    assert result["result_data"]["refs"].startswith("- button")
    assert result["result_data"]["navigated"] is True
    assert result["degradation_log"] == []
    assert result["stop_reason"] == "completed"


@pytest.mark.asyncio
async def test_browser_task_without_navigable_url_is_partial(tmp_path):
    """C2b：任务里没有可导航的 URL（open 失败）→ 只观察了当时开着的页面，必须标 partial。

    "完成了"不能用来描述"只是看了一眼当时正好开着的页面"：那个页面与用户目标的匹配关系
    从未被验证（它可能是上一次运行/别的会话留下的）——未与目标匹配的快照不得报 completed。
    """
    tracer = JsonlTracer(trace_id="t17", out_dir=tmp_path / "traces")
    ex = BrowserExecutor(page_open=True, open_error="invalid_url")
    provider = StubProvider()
    graph = build_graph(ex, tracer, provider)
    result = await graph.ainvoke(AgentState(task="帮我点击登录按钮"))
    assert [name for name, _ in ex.calls] == ["browser_open", "browser_snapshot"]
    assert result["result_data"]["refs"].startswith("- button")  # 页面内容仍然如实交出
    assert result["result_data"]["navigated"] is False
    assert result["result_data"]["partial"] is True
    assert result["result_data"]["partial_reason"] == "no_navigation_for_task"
    assert result["stop_reason"] == "partial"  # ← 不得报 completed
    # 降级记录如实：导航确实失败了（不是假失败，是真没导航）
    assert [d["strategy"] for d in result["degradation_log"]] == ["browser_open"]
    assert result["degradation_log"][0]["error"] == "invalid_url"
    # 答案模型被告知这次是 partial，不许说任务完成
    assert "partial" in provider.last_user_content
    assert "没有完成与任务目标匹配" in provider.last_user_content


@pytest.mark.asyncio
async def test_browser_task_opened_but_unobserved_is_partial(tmp_path):
    """C2：导航成功但观察步失败 → 数据只有"打开了哪个页面"，没有页面内容 —— 仍不得报 completed。

    任务目标在页面上；只报"已打开 URL"对"打开它并告诉我…"类任务就是假成功。
    """
    tracer = JsonlTracer(trace_id="t18", out_dir=tmp_path / "traces")
    ex = BrowserExecutor(page_open=False, snapshot_error="empty_snapshot")
    provider = StubProvider()
    graph = build_graph(ex, tracer, provider)
    result = await graph.ainvoke(AgentState(task="帮我比价 iPhone 15 和 小米 15"))
    assert [name for name, _ in ex.calls] == ["browser_open", "browser_snapshot"]
    assert result["result_data"]["url"] == "https://example.com"  # 导航数据保留
    assert result["result_data"]["navigated"] is True
    assert result["result_data"]["opened_url"] == "https://example.com"
    assert result["result_data"]["partial"] is True
    assert result["result_data"]["partial_reason"] == "no_observation_after_navigation"
    assert result["stop_reason"] == "partial"
    assert [d["strategy"] for d in result["degradation_log"]] == ["browser_snapshot"]
    assert "没有完成与任务目标匹配" in provider.last_user_content


@pytest.mark.asyncio
async def test_browser_task_without_browser_degrades_honestly(tmp_path):
    """浏览器不可用：两条策略都记 browser_unavailable → all_strategies_failed + 明确告知。"""
    tracer = JsonlTracer(trace_id="t12", out_dir=tmp_path / "traces")
    graph = build_graph(BrowserExecutor(browser_error="browser_unavailable"), tracer, StubProvider())
    result = await graph.ainvoke(AgentState(task="帮我点击登录按钮"))
    assert result["result_data"]["error"] == "all_strategies_failed"
    degradation = result["result_data"]["degradation"]
    assert [d["strategy"] for d in degradation] == ["browser_open", "browser_snapshot"]
    assert all(d["error"] == "browser_unavailable" for d in degradation)
    assert result["degradation_log"] == degradation
    assert "无法完成" in result["final_answer"]
    assert "browser_unavailable" in result["final_answer"]


@pytest.mark.asyncio
async def test_browser_task_surfaces_policy_reason_in_answer(tmp_path):
    """policy 拒绝原因（稳定契约）必须原样进降级链与最终回答，绝不吞掉。"""

    class BlockedExecutor(StubExecutor):
        async def call(self, name, arguments):
            if name.startswith("browser_"):
                return ToolResult(
                    status="error",
                    error_type="navigation_blocked",
                    error_message="navigation_blocked: protocol_blocked: file: is not allowed",
                )
            return await super().call(name, arguments)

    tracer = JsonlTracer(trace_id="t13", out_dir=tmp_path / "traces")
    graph = build_graph(BlockedExecutor(), tracer, StubProvider())
    result = await graph.ainvoke(AgentState(task="帮我点击登录按钮"))
    assert [d["error"] for d in result["degradation_log"]] == [
        "navigation_blocked",
        "navigation_blocked",
    ]
    assert "protocol_blocked" in result["final_answer"]


@pytest.mark.asyncio
async def test_read_page_escalates_to_browser_when_fetch_fails(tmp_path):
    """tier2 失败升级到 tier3：HTTP 抓取不行就用真浏览器（同一意图，降级链回退）。"""
    tracer = JsonlTracer(trace_id="t14", out_dir=tmp_path / "traces")
    ex = FetchFailingExecutor()
    graph = build_graph(ex, tracer, StubProvider())
    result = await graph.ainvoke(AgentState(task="打开 https://www.python.org 告诉我页面主要内容"))
    assert result["intent"] == "read_page"
    assert result["tier"] == "tier2"
    assert [name for name, _ in ex.calls] == ["fetch_page", "browser_open"]
    assert result["result_data"]["url"] == "https://example.com"
    assert [d["strategy"] for d in result["degradation_log"]] == ["fetch_page"]
    assert result["stop_reason"] == "completed"


@pytest.mark.asyncio
async def test_read_page_keeps_fetch_page_when_it_succeeds(tmp_path):
    """升级只在失败后发生：fetch_page 成功就不碰浏览器（现有 tier2 行为不变）。"""
    tracer = JsonlTracer(trace_id="t15", out_dir=tmp_path / "traces")
    ex = RecordingExecutor()
    graph = build_graph(ex, tracer, StubProvider())
    result = await graph.ainvoke(AgentState(task="打开 https://www.python.org 告诉我页面主要内容"))
    assert [name for name, _ in ex.calls] == ["fetch_page"]
    assert result["degradation_log"] == []
    assert result["result_data"] == {}  # StubExecutor 的 fetch_page 回显空 data
    assert result["stop_reason"] == "completed"
