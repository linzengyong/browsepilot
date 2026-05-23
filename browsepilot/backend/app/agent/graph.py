"""LangGraph StateGraph construction for BrowsePilot agent."""

from loguru import logger

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from backend.app.agent.state import AgentState
from backend.app.config import settings
from backend.app.agent.nodes import (
    plan_node, execute_node, reflect_node, replan_node, answer_node,
    classify_node, pre_observe_node,
)
from backend.app.mcp_client import MCPClient


def build_graph(mcp_client: MCPClient, lazy_mcp: bool = False):
    """Build and compile the BrowsePilot agent StateGraph."""
    workflow = StateGraph(AgentState)

    async def classify(state: AgentState) -> dict:
        return await classify_node(state)

    async def pre_observe(state: AgentState) -> dict:
        # pre_observe is the first node that calls MCP tools, so in lazy_mcp mode
        # it must be the one to establish the connection
        if lazy_mcp and not mcp_client.is_connected:
            await mcp_client.connect()
        return await pre_observe_node(state, mcp_client)

    async def plan(state: AgentState) -> dict:
        return await plan_node(state, mcp_client)

    async def execute(state: AgentState) -> dict:
        return await execute_node(state, mcp_client, mcp_client.tools)

    async def reflect(state: AgentState) -> dict:
        return await reflect_node(state)

    async def replan(state: AgentState) -> dict:
        return await replan_node(state, mcp_client)

    async def answer(state: AgentState) -> dict:
        return await answer_node(state)

    # Add nodes
    workflow.add_node("classify", classify)
    workflow.add_node("pre_observe", pre_observe)
    workflow.add_node("plan", plan)
    workflow.add_node("execute", execute)
    workflow.add_node("reflect", reflect)
    workflow.add_node("replan", replan)
    workflow.add_node("answer", answer)

    # Entry: classify instead of plan
    workflow.set_entry_point("classify")

    # classify → conditional routing
    workflow.add_conditional_edges(
        "classify",
        _route_classify,
        {
            "chitchat": "answer",
            "knowledge_qa": "answer",
            "browser_task": "pre_observe",
        },
    )

    workflow.add_edge("pre_observe", "plan")
    workflow.add_edge("plan", "execute")
    workflow.add_edge("execute", "reflect")
    workflow.add_edge("replan", "execute")
    workflow.add_edge("answer", END)

    workflow.add_conditional_edges(
        "reflect",
        _route_reflect,
        {
            "execute": "execute",
            "replan": "replan",
            "answer": "answer",
        },
    )

    return workflow.compile(checkpointer=MemorySaver())


def _route_classify(state: AgentState) -> str:
    """Route after classification: chitchat/knowledge_qa → answer, browser_task → pre_observe."""
    intent = state.get("intent", "browser_task")
    if intent in ("chitchat", "knowledge_qa"):
        return intent
    return "browser_task"


def _route_reflect(state: AgentState) -> str:
    """Route after reflection: circuit breaker checks, then retry/continue/replan/done."""
    # Circuit breaker checks
    if state.get("consecutive_failures", 0) >= settings.consecutive_failures_threshold:
        logger.warning("Circuit breaker: {} consecutive failures, forcing answer", state["consecutive_failures"])
        return "answer"
    if state.get("replan_count", 0) >= settings.replan_max_count:
        logger.warning("Too many replans ({}), forcing answer", state["replan_count"])
        return "answer"
    if state.get("stagnation_count", 0) >= settings.stagnation_threshold:
        logger.warning("Stagnation detected (count={}), forcing answer", state["stagnation_count"])
        return "answer"

    # Step limit is now handled by reflect_node (dual-layer: plan_step_count + total_steps).
    # No redundant check here — reflect_node sets stop_reason before routing.

    # Original routing logic
    if state.get("need_replan"):
        return "replan"
    if state.get("plan") and len(state["plan"]) > 0:
        return "execute"
    return "answer"
