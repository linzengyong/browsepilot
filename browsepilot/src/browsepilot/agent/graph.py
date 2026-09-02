"""Agent graph: classify -> (execute ->) answer.

classify is hybrid rules-first (article 3): cheap keyword/regex rules win;
the small model is the fallback via provider.structured; a final fallback
defaults to tier1/search. execute runs a per-intent strategy chain
(_STRATEGY_CHAIN): each attempt extracts params via provider.structured
(weather→search fallback reuses the raw task text as query), calls the
ToolExecutor, and records every failed attempt in degradation_log until a
strategy succeeds or all fail (all_strategies_failed). answer synthesizes
the final reply from result_data (chitchat/knowledge_qa answer directly;
tool failures are explained explicitly, never fabricated).
"""

from __future__ import annotations

import json
from typing import cast

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel

from browsepilot.agent.params import ReadPageParams, SearchParams, WeatherParams
from browsepilot.agent.router import IntentResult, RouteDecision, classify_by_rules
from browsepilot.agent.state import AgentState
from browsepilot.agent.tools import ToolExecutor
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

    # 降级策略链：weather 城市未命中 → 用 task 原文搜索兑底；
    # search/read_page 无有意义回退，失败即明确降级（all_strategies_failed）。
    _STRATEGY_CHAIN: dict[str, list[str]] = {
        "weather": ["weather", "search"],
        "search": ["search"],
        "read_page": ["fetch_page"],
    }

    _PARAM_MODELS: dict[str, type[BaseModel]] = {
        "weather": WeatherParams,
        "search": SearchParams,
        "fetch_page": ReadPageParams,
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
        """策略链循环：按 intent 的顺序尝试工具，每次失败记 degradation。

        首次尝试按 intent 的 Params 结构化提取；天气回退到 search 时不再
        结构化提取，直接用 task 原文作 query。全败 → result_data 返回
        all_strategies_failed + degradation；全程 tool_call trace 可追溯。
        """
        with tracer.span("execute", intent=state.intent):
            chain = _STRATEGY_CHAIN.get(state.intent, ["search"])
            degradation: list[dict] = []
            for tool_name in chain:
                params_model = _PARAM_MODELS[tool_name]
                try:
                    if tool_name == "search" and state.intent == "weather" and degradation:
                        # 天气城市未命中 → 用 task 原文搜索兑底，不再结构化提取
                        params: BaseModel = SearchParams(query=state.task)
                    else:
                        params = await provider.structured(
                            [
                                Message(role="system", content="从用户任务中提取参数。"),
                                Message(role="user", content=state.task),
                            ],
                            schema=params_model,
                            model=small_model,
                        )
                except StructuredOutputError as exc:
                    degradation.append(
                        {
                            "strategy": tool_name,
                            "error": "param_extraction_failed",
                            "message": str(exc),
                        }
                    )
                    continue
                tool_result = await executor.call(tool_name, params.model_dump())
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
                    if tool_result.data.get("needs_human"):
                        # HITL：工具需要人工确认 → 交给 hitl 节点 interrupt
                        return {
                            "result_data": tool_result.data,
                            "pending_human_action": True,
                            "human_prompt": str(tool_result.data.get("human_prompt", "")),
                            "degradation_log": state.degradation_log + degradation,
                        }
                    return {
                        "result_data": tool_result.data,
                        "stop_reason": "",
                        "degradation_log": state.degradation_log + degradation,
                    }
                degradation.append(
                    {
                        "strategy": tool_name,
                        "error": tool_result.error_type,
                        "message": tool_result.error_message,
                    }
                )
            return {
                "result_data": {"error": "all_strategies_failed", "degradation": degradation},
                "degradation_log": state.degradation_log + degradation,
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
                message = state.result_data.get("message")
                if not message:
                    # all_strategies_failed 形状：逐条拼出降级原因
                    message = "；".join(
                        f"{d.get('strategy', '')}: {d.get('message', '')}"
                        for d in state.result_data.get("degradation", [])
                    ) or state.result_data["error"]
                text = f"抱歉，这个任务暂时无法完成：{message}。可以换个说法试试。"
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

    async def hitl(state: AgentState) -> dict:
        """Human-in-the-loop：interrupt 暂停执行，resume 后带上人工确认回 answer。"""
        with tracer.span("hitl"):
            response = interrupt({"human_prompt": state.human_prompt})
            return {
                "human_response": str(response),
                "pending_human_action": False,
                "hitl_triggered_count": state.hitl_triggered_count + 1,
            }

    def _route_execute_result(state: AgentState) -> str:
        if state.pending_human_action and state.hitl_triggered_count == 0:
            return "hitl"
        return "answer"

    def _route_after_classify(state: AgentState) -> str:
        return "answer" if state.intent in ("chitchat", "knowledge_qa") else "execute"

    workflow.add_node("classify", classify)
    workflow.add_node("execute", execute)
    workflow.add_node("hitl", hitl)
    workflow.add_node("answer", answer)
    workflow.set_entry_point("classify")
    workflow.add_conditional_edges(
        "classify", _route_after_classify, {"execute": "execute", "answer": "answer"}
    )
    workflow.add_conditional_edges(
        "execute", _route_execute_result, {"hitl": "hitl", "answer": "answer"}
    )
    workflow.add_edge("hitl", "answer")
    workflow.add_edge("answer", END)
    return workflow.compile(checkpointer=checkpointer)
