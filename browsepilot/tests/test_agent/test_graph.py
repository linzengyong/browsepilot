import pytest

from browsepilot.agent.graph import build_graph
from browsepilot.agent.state import AgentState
from browsepilot.agent.tools import ToolResult, ToolSpec
from browsepilot.agent.tracer import JsonlTracer


class StubExecutor:
    async def list_tools(self):
        return [ToolSpec(name="echo", description="echo back")]

    async def call(self, name, arguments):
        return ToolResult(content="stub")


@pytest.mark.asyncio
async def test_skeleton_graph_runs_and_traces(tmp_path):
    tracer = JsonlTracer(trace_id="test-trace", out_dir=tmp_path / "traces")
    graph = build_graph(StubExecutor(), tracer)
    result = await graph.ainvoke(AgentState(task="查天气"))
    # LangGraph 1.x ainvoke returns a dict (see plan T9 note)
    assert result["final_answer"].startswith("[P0 stub]")
    assert "查天气" in result["final_answer"]
    assert result["intent"] == "browser_task"
    assert result["tier"] == "tier1"
    trace_path = tmp_path / "traces" / "test-trace.jsonl"
    lines = trace_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4  # classify start/end + answer start/end
