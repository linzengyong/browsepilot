"""P0 agent graph skeleton: classify -> answer.

No LLM and no real tools yet — this task proves the LangGraph + Pydantic
state + Tracer wiring works end to end.
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from browsepilot.agent.state import AgentState
from browsepilot.agent.tools import ToolExecutor
from browsepilot.agent.tracer import Tracer


def build_graph(executor: ToolExecutor, tracer: Tracer):
    workflow = StateGraph(AgentState)

    def classify(state: AgentState) -> dict:
        with tracer.span("classify", task=state.task[:60]):
            return {"intent": "browser_task", "tier": "tier1"}

    def answer(state: AgentState) -> dict:
        with tracer.span("answer"):
            return {
                "final_answer": f"[P0 stub] task received: {state.task}",
                "stop_reason": "stub",
            }

    workflow.add_node("classify", classify)
    workflow.add_node("answer", answer)
    workflow.set_entry_point("classify")
    workflow.add_edge("classify", "answer")
    workflow.add_edge("answer", END)
    return workflow.compile()
