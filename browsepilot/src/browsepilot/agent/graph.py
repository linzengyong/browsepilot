"""Agent graph: classify -> (execute ->) answer.

classify is hybrid rules-first (article 3): cheap keyword/regex rules win;
the small model is the fallback via provider.structured; a final fallback
defaults to tier1/search. execute extracts tool params via
provider.structured and calls the ToolExecutor; answer synthesizes the
final reply from result_data (chitchat/knowledge_qa answer directly;
tool failures are explained explicitly, never fabricated).
"""

from __future__ import annotations

import json
from typing import cast

from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from browsepilot.agent.params import ReadPageParams, SearchParams, WeatherParams
from browsepilot.agent.router import IntentResult, RouteDecision, classify_by_rules
from browsepilot.agent.state import AgentState
from browsepilot.agent.tools import ToolExecutor, ToolResult
from browsepilot.agent.tracer import TraceEvent, Tracer
from browsepilot.model.messages import ChatResult, Message
from browsepilot.model.provider import ModelProvider
from browsepilot.model.structured import StructuredOutputError


def build_graph(
    executor: ToolExecutor,
    tracer: Tracer,
    provider: ModelProvider,
    *,
    small_model: str | None = None,
    checkpointer=None,
):
    workflow = StateGraph(AgentState)

    _TOOL_BY_INTENT: dict[str, tuple[str, type[BaseModel]]] = {
        "weather": ("weather", WeatherParams),
        "search": ("search", SearchParams),
        "read_page": ("fetch_page", ReadPageParams),
    }

    async def classify(state: AgentState) -> dict:
        with tracer.span("classify", task=state.task[:60]):
            decision = classify_by_rules(state.task)
            if decision is None:
                try:
                    result = cast(
                        IntentResult,
                        await provider.structured(
                            [
                                Message(role="system", content="你是意图分类器。"),
                                Message(role="user", content=state.task),
                            ],
                            schema=IntentResult,
                            model=small_model,
                        ),
                    )
                    decision = RouteDecision(intent=result.intent, tier=result.tier, confidence=0.8)
                except StructuredOutputError:
                    # fallback: search is the safest default for an info product
                    decision = RouteDecision(intent="search", tier="tier1", confidence=0.0)
            tracer.emit(
                TraceEvent(
                    trace_id=tracer.trace_id,
                    span_id=tracer.new_span_id(),
                    node="classify",
                    event_type="decision",
                    data=decision.model_dump(),
                )
            )
            return {"intent": decision.intent, "tier": decision.tier}

    async def execute(state: AgentState) -> dict:
        with tracer.span("execute", intent=state.intent):
            tool_name, params_model = _TOOL_BY_INTENT[state.intent]
            try:
                params = await provider.structured(
                    [
                        Message(role="system", content="从用户任务中提取参数。"),
                        Message(role="user", content=state.task),
                    ],
                    schema=params_model,
                    model=small_model,
                )
                tool_result = await executor.call(tool_name, params.model_dump())
            except StructuredOutputError as exc:
                tool_result = ToolResult(
                    status="error", error_type="param_extraction_failed", error_message=str(exc)
                )
            tracer.emit(
                TraceEvent(
                    trace_id=tracer.trace_id,
                    span_id=tracer.new_span_id(),
                    node="execute",
                    event_type="tool_call",
                    data={"tool": tool_name, "status": tool_result.status},
                )
            )
            if tool_result.status == "success":
                return {"result_data": tool_result.data, "stop_reason": ""}
            return {
                "result_data": {
                    "error": tool_result.error_type,
                    "message": tool_result.error_message,
                }
            }

    async def answer(state: AgentState) -> dict:
        with tracer.span("answer"):
            if state.intent in ("chitchat", "knowledge_qa"):
                # 直答路径同样把历史摘要放进上下文；无历史时保持原有 prompt
                if state.history:
                    history_text = "\n".join(state.history[-5:])
                    user_content = f"历史对话：\n{history_text}\n\n任务：{state.task}"
                else:
                    user_content = state.task
                chat_result: ChatResult = await provider.chat(
                    [Message(role="user", content=user_content)]
                )
                text = chat_result.text
            elif state.result_data.get("error"):
                text = f"抱歉，这个任务暂时无法完成：{state.result_data['message']}。可以换个说法试试。"
            else:
                history_text = "\n".join(state.history[-5:]) if state.history else "（无历史）"
                answer_result: ChatResult = await provider.chat(
                    [
                        Message(
                            role="system",
                            content="你是信息助手。用中文简洁回答，基于给定数据，不要编造。",
                        ),
                        Message(
                            role="user",
                            content=(
                                f"历史对话：\n{history_text}\n\n"
                                f"任务：{state.task}\n数据：{json.dumps(state.result_data, ensure_ascii=False)}"
                            ),
                        ),
                    ]
                )
                text = answer_result.text
            return {"final_answer": text, "stop_reason": "completed"}

    def _route_after_classify(state: AgentState) -> str:
        return "answer" if state.intent in ("chitchat", "knowledge_qa") else "execute"

    workflow.add_node("classify", classify)
    workflow.add_node("execute", execute)
    workflow.add_node("answer", answer)
    workflow.set_entry_point("classify")
    workflow.add_conditional_edges(
        "classify", _route_after_classify, {"execute": "execute", "answer": "answer"}
    )
    workflow.add_edge("execute", "answer")
    workflow.add_edge("answer", END)
    return workflow.compile(checkpointer=checkpointer)
