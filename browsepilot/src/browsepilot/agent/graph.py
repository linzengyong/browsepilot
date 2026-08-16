"""Agent graph: classify -> answer.

classify is hybrid rules-first (article 3): cheap keyword/regex rules win;
the small model is the fallback via provider.structured; a final fallback
defaults to tier1/search. answer is still the P0 stub (T7 rewrites it).
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from browsepilot.agent.router import IntentResult, RouteDecision, classify_by_rules
from browsepilot.agent.state import AgentState
from browsepilot.agent.tools import ToolExecutor
from browsepilot.agent.tracer import TraceEvent, Tracer
from browsepilot.model.messages import Message
from browsepilot.model.provider import ModelProvider
from browsepilot.model.structured import StructuredOutputError


def build_graph(
    executor: ToolExecutor,
    tracer: Tracer,
    provider: ModelProvider,
    *,
    small_model: str | None = None,
):
    workflow = StateGraph(AgentState)

    async def classify(state: AgentState) -> dict:
        with tracer.span("classify", task=state.task[:60]):
            decision = classify_by_rules(state.task)
            if decision is None:
                try:
                    result = await provider.structured(
                        [Message(role="system", content="你是意图分类器。"),
                         Message(role="user", content=state.task)],
                        schema=IntentResult,
                        model=small_model,
                    )
                    decision = RouteDecision(intent=result.intent, tier=result.tier, confidence=0.8)
                except StructuredOutputError:
                    # fallback: search is the safest default for an info product
                    decision = RouteDecision(intent="search", tier="tier1", confidence=0.0)
            tracer.emit(TraceEvent(trace_id=tracer.trace_id, span_id=tracer.new_span_id(),
                                   node="classify", event_type="decision",
                                   data=decision.model_dump()))
            return {"intent": decision.intent, "tier": decision.tier}

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
