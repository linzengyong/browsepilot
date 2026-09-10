import json

import pytest

from browsepilot.agent.graph import _merge_degradation, build_graph
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
    assert resumed["stop_reason"] == "completed"


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
    """browser_task 链的替身：默认“没有打开的页面”（snapshot 失败 → open 兜底）。"""

    def __init__(self, *, page_open: bool = False, browser_error: str = "") -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self.page_open = page_open
        self.browser_error = browser_error

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name.startswith("browser_"):
            if self.browser_error:  # 模拟真实的"未注入 BrowserPool"：每个浏览器工具都降级
                return ToolResult(
                    status="error",
                    error_type=self.browser_error,
                    error_message=f"浏览器工具失败：{self.browser_error}",
                )
            if name == "browser_snapshot":
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
                    data={"url": "https://example.com/", "refs": refs},
                )
            if name == "browser_open":
                self.page_open = True
                url = arguments.get("url", "")
                return ToolResult(status="success", content=f"已打开 {url}", data={"url": url})
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
async def test_browser_task_snapshots_first_then_opens(tmp_path):
    """tier3 链：先看当前页（点/填都建立在已打开的页面上），没有页面才 open。"""
    tracer = JsonlTracer(trace_id="t10", out_dir=tmp_path / "traces")
    ex = BrowserExecutor(page_open=False)
    provider = StubProvider()
    graph = build_graph(ex, tracer, provider)
    result = await graph.ainvoke(AgentState(task="帮我比价 iPhone 15 和 小米 15"))
    assert result["intent"] == "browser_task"
    assert result["tier"] == "tier3"
    assert [name for name, _ in ex.calls] == ["browser_snapshot", "browser_open"]
    assert ex.calls[0][1] == {}  # snapshot 无参数：不需要模型提取
    assert ex.calls[1][1]["url"] == "https://example.com"
    assert result["result_data"]["url"] == "https://example.com"
    assert [d["strategy"] for d in result["degradation_log"]] == ["browser_snapshot"]
    assert result["degradation_log"][0]["error"] == "no_open_page"
    assert result["stop_reason"] == "completed"
    # 两次尝试都有 tool_call 跟踪（失败一次、成功一次）
    lines = (tmp_path / "traces" / "t10.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    tool_calls = [e for e in events if e["event_type"] == "tool_call"]
    assert [(e["data"]["tool"], e["data"]["status"]) for e in tool_calls] == [
        ("browser_snapshot", "error"),
        ("browser_open", "success"),
    ]


@pytest.mark.asyncio
async def test_browser_task_prefers_current_page_snapshot(tmp_path):
    """已有打开的页面：snapshot 即成功 → 链不继续（不再重复导航）。"""
    tracer = JsonlTracer(trace_id="t11", out_dir=tmp_path / "traces")
    ex = BrowserExecutor(page_open=True)
    provider = StubProvider()
    graph = build_graph(ex, tracer, provider)
    result = await graph.ainvoke(AgentState(task="帮我点击登录按钮"))
    assert [name for name, _ in ex.calls] == ["browser_snapshot"]
    assert result["result_data"]["refs"].startswith("- button")
    assert result["degradation_log"] == []
    assert provider.structured_calls == 0  # 规则命中 + snapshot 无参数 → 零模型调用


@pytest.mark.asyncio
async def test_browser_task_without_browser_degrades_honestly(tmp_path):
    """浏览器不可用：两条策略都记 browser_unavailable → all_strategies_failed + 明确告知。"""
    tracer = JsonlTracer(trace_id="t12", out_dir=tmp_path / "traces")
    graph = build_graph(BrowserExecutor(browser_error="browser_unavailable"), tracer, StubProvider())
    result = await graph.ainvoke(AgentState(task="帮我点击登录按钮"))
    assert result["result_data"]["error"] == "all_strategies_failed"
    degradation = result["result_data"]["degradation"]
    assert [d["strategy"] for d in degradation] == ["browser_snapshot", "browser_open"]
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
