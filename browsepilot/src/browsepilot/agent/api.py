"""Agent entry point shared by the HTTP API and the eval harness."""

from __future__ import annotations

from browsepilot.agent.graph import build_graph
from browsepilot.agent.state import AgentState
from browsepilot.agent.tools import ToolExecutor
from browsepilot.agent.tracer import Tracer
from browsepilot.model.provider import ModelProvider


async def run_task(
    task: str,
    *,
    provider: ModelProvider,
    executor: ToolExecutor,
    tracer: Tracer,
    session_id: str = "",
) -> dict:
    """Run one user task through the graph. Returns the state as a dict."""
    graph = build_graph(executor, tracer, provider)
    return await graph.ainvoke(AgentState(task=task, session_id=session_id))
