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
    history: list[str] | None = None,
) -> dict:
    """Run one user task through the graph. Returns the state as a dict.

    history: 已格式化的历史摘要（"Q: … / A: …"，最多 5 条、截断 ~200 字符），
    注入本轮 AgentState，让 answer 节点带多轮上下文合成回答。
    """
    graph = build_graph(executor, tracer, provider)
    return await graph.ainvoke(
        AgentState(task=task, session_id=session_id, history=history or [])
    )
