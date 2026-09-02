"""Agent entry point shared by the HTTP API and the eval harness."""

from __future__ import annotations

from browsepilot.agent.graph import build_graph
from browsepilot.agent.state import AgentState
from browsepilot.agent.tools import ToolExecutor
from browsepilot.agent.tracer import Tracer
from browsepilot.agent.tracer_provider import TracingModelProvider
from browsepilot.model.provider import ModelProvider


async def run_task(
    task: str,
    *,
    provider: ModelProvider,
    executor: ToolExecutor,
    tracer: Tracer,
    session_id: str = "",
    history: list[str] | None = None,
    checkpointer=None,
    config: dict | None = None,
) -> dict:
    """Run one user task through the graph. Returns the state as a dict.

    history: 已格式化的历史摘要（"Q: … / A: …"，最多 5 条、截断 ~200 字符），
    注入本轮 AgentState，让 answer 节点带多轮上下文合成回答。

    checkpointer: 传入时按 checkpoint 持久化（HITL interrupt 暂停必需）；
    config 携带 thread_id（通常 = session_id）供后续 resume_task 续跑。
    """
    graph = build_graph(
        executor,
        tracer,
        TracingModelProvider(provider, tracer, session_id),
        checkpointer=checkpointer,
    )
    return await graph.ainvoke(
        AgentState(task=task, session_id=session_id, history=history or []),
        config=config,
    )


async def resume_task(
    session_id: str,
    human_response: str,
    *,
    provider: ModelProvider,
    executor: ToolExecutor,
    tracer: Tracer,
    checkpointer=None,
) -> dict:
    """Resume a run paused at a HITL interrupt. Returns the state as a dict.

    Guard: 无待处理 interrupt 时 LangGraph 会用 resume 值静默开一次全新运行，
    所以这里先 aget_state 校验（next 非空且 pending_human_action 为真），
    否则抛 RuntimeError，而不是误开新任务。

    返回结果额外带回原始 task（从 checkpoint state values 取），供调用方
    （/chat/resume）按 task+final_answer 落库。
    """
    from langgraph.types import Command

    graph = build_graph(
        executor,
        tracer,
        TracingModelProvider(provider, tracer, session_id),
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": session_id}}
    snapshot = await graph.aget_state(config)
    if not snapshot.next or not snapshot.values.get("pending_human_action"):
        raise RuntimeError(
            f"session {session_id!r} has no pending human action to resume"
        )
    result = await graph.ainvoke(Command(resume=human_response), config=config)
    return {**result, "task": snapshot.values.get("task") or result.get("task", "")}


async def has_pending_human_action(
    thread_id: str,
    *,
    provider: ModelProvider,
    executor: ToolExecutor,
    tracer: Tracer,
    checkpointer=None,
) -> bool:
    """True 当线程暂停在 HITL interrupt（checkpoint next 非空且
    pending_human_action 为真）。/chat/stream 用它拒绝在待确认线程上
    静默开新任务（H4）。无 checkpointer 或线程不存在时为 False。
    """
    if checkpointer is None:
        return False
    graph = build_graph(executor, tracer, provider, checkpointer=checkpointer)
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return bool(snapshot.next) and bool(snapshot.values.get("pending_human_action"))
