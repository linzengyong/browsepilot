import json

import pytest

from browsepilot.agent.graph import build_graph
from browsepilot.agent.router import IntentResult
from browsepilot.agent.state import AgentState
from browsepilot.agent.tools import ToolResult, ToolSpec
from browsepilot.agent.tracer import JsonlTracer


class StubExecutor:
    async def list_tools(self):
        return [ToolSpec(name="echo", description="echo back")]

    async def call(self, name, arguments):
        return ToolResult(content="stub")


class StubProvider:
    """Model provider stub: structured always classifies as weather."""

    def __init__(self):
        self.structured_calls = 0

    async def chat(self, messages, *, model=None):
        raise NotImplementedError

    async def stream(self, messages, *, model=None):
        raise NotImplementedError

    async def structured(self, messages, *, schema, model=None, max_retries=3):
        self.structured_calls += 1
        return IntentResult(intent="weather", tier="tier1")


@pytest.mark.asyncio
async def test_skeleton_graph_runs_and_traces(tmp_path):
    tracer = JsonlTracer(trace_id="test-trace", out_dir=tmp_path / "traces")
    provider = StubProvider()
    graph = build_graph(StubExecutor(), tracer, provider)
    result = await graph.ainvoke(AgentState(task="查天气"))
    # LangGraph 1.x ainvoke returns a dict (see plan T9 note)
    assert result["final_answer"].startswith("[P0 stub]")
    assert "查天气" in result["final_answer"]
    # "查天气" hits the weather rule -> rules-first, no model call
    assert result["intent"] == "weather"
    assert result["tier"] == "tier1"
    assert provider.structured_calls == 0
    trace_path = tmp_path / "traces" / "test-trace.jsonl"
    lines = trace_path.read_text(encoding="utf-8").strip().splitlines()
    # classify start/end + decision + answer start/end
    assert len(lines) == 5
    events = [json.loads(line) for line in lines]
    decisions = [e for e in events if e["event_type"] == "decision"]
    assert len(decisions) == 1
    assert decisions[0]["node"] == "classify"
    assert decisions[0]["data"]["intent"] == "weather"
    assert decisions[0]["data"]["tier"] == "tier1"
    assert decisions[0]["data"]["confidence"] == 1.0


@pytest.mark.asyncio
async def test_graph_falls_back_to_model_when_rules_miss(tmp_path):
    tracer = JsonlTracer(trace_id="test-trace-fallback", out_dir=tmp_path / "traces")
    provider = StubProvider()
    graph = build_graph(StubExecutor(), tracer, provider)
    result = await graph.ainvoke(AgentState(task="帮我处理一下那个事情"))
    # rules miss -> small model classifies (stub returns weather/tier1)
    assert result["intent"] == "weather"
    assert result["tier"] == "tier1"
    assert provider.structured_calls == 1
