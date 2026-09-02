import json

import pytest

from browsepilot.agent.graph import build_graph
from browsepilot.agent.router import IntentResult
from browsepilot.agent.state import AgentState
from browsepilot.agent.tools import ToolResult, ToolSpec
from browsepilot.agent.tracer import JsonlTracer
from browsepilot.model.messages import ChatResult


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
async def test_tool_failure_degrades_with_explicit_reason(tmp_path):
    tracer = JsonlTracer(trace_id="t4", out_dir=tmp_path / "traces")
    graph = build_graph(FailingExecutor(), tracer, StubProvider())
    result = await graph.ainvoke(AgentState(task="北京天气怎么样？"))
    assert result["intent"] == "weather"
    assert result["result_data"]["error"] == "service_error"
    assert "无法完成" in result["final_answer"]
    assert "boom" in result["final_answer"]
