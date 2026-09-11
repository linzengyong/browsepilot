"""Agent graph: classify -> (execute ->) answer.

classify is hybrid rules-first (article 3): cheap keyword/regex rules win;
the small model is the fallback via provider.structured; a final fallback
defaults to tier1/search. execute runs a per-intent strategy chain
(_STRATEGY_CHAIN): each attempt extracts params via provider.structured
(weather→search fallback reuses the raw task text as query), calls the
ToolExecutor, and records every failed attempt in degradation_log until a
strategy succeeds or all fail (all_strategies_failed). answer synthesizes
the final reply from result_data (chitchat/knowledge_qa answer directly;
tool failures are explained explicitly, never fabricated). Answers built on a
degraded (fallback) path must disclose it (spec §2 「带说明的降级回答」): the payload
carries ``degraded_from`` and the system prompt requires the model to state what
failed and why, and never to present the fallback result as the answer to the
original question.
"""

from __future__ import annotations

import json
from typing import cast

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel

from browsepilot.agent.params import BrowserParams, ReadPageParams, SearchParams, WeatherParams
from browsepilot.agent.router import IntentResult, RouteDecision, classify_by_rules
from browsepilot.agent.state import AgentState
from browsepilot.agent.tools import ToolExecutor
from browsepilot.agent.tracer import TraceEvent, Tracer
from browsepilot.model.messages import ChatResult, Message
from browsepilot.model.provider import ModelProvider
from browsepilot.model.structured import StructuredOutputError


def _human_feedback_note(human_response: str) -> str:
    """resume 回来的人工反馈 → 答案 prompt 里的一行（C1）；空串 = 本轮没有人工介入。

    人工反馈不是工具结果，所以不塞进 ``result_data``（那是工具产出的数据面）：
    它是 ``human_prompt`` 的另一面——一个是"请人工去做 X"，一个是"人工做完了"。
    """
    if not human_response:
        return ""
    return f"\n人工反馈（人工已完成上一步操作）：{human_response}"


def _answer_payload(state: AgentState) -> dict:
    """进答案模型的数据（C1 / F4）：HITL 的**控制面**字段不进去。

    ``human_prompt`` 是**打断时对人工的请求**（“请在浏览器中手动登录后继续任务。”），
    ``intervention`` 是它的机器可读副本（``"login"`` / ``"captcha"`` 这类稳定码，也可能只是
    medium 置信度“只记录不暂停”的痕迹）；人工反馈到了就再也没人要做这件事了，
    留着会把模型带回“再请用户手动登录一次”的老路（P3 final review C1：resume 白做），
    所以 resume 之后连它们一起换成人工反馈本身（``human_response``）。
    原始字段仍留在 state 的 ``result_data`` 与 trace 里（诊断不丢）。

    ``needs_human`` 是“转 hitl 节点暂停”的控制面开关（``graph.execute`` 看它决定要不要 interrupt），
    不是给答案模型的数据：resume 之后人已经做完了，agent 的下一步是交答案而不是再请求一次暂停
    （P3 收口 re-review F4：光靠 system 里那句“人工已经做完”挡不住数据里的“需要人工”）——
    所以**无条件**剥掉。

    降级**后仍拿到数据**的路径另注入 ``degraded_from``（T5 跟进 / spec §2「带说明的降级回答」）：
    兜底成功时模型只看到“一份成功的数据”，没人告诉它这份数据是绕道来的，它就会把兜底结果当作
    原问题的答案（P4-T5 真实模式实证：把搜到的“三亚亚特兰蒂斯水世界”的天气答成用户问的
    “亚特兰蒂斯”的天气）。全败路径不注入（见 :func:`_degraded_provenance`）。
    """
    data = dict(state.result_data)
    data.pop("needs_human", None)
    if state.human_response:
        data.pop("human_prompt", None)
        data.pop("intervention", None)
        data["human_response"] = state.human_response
    degraded_from = _degraded_provenance(state)
    if degraded_from:
        data["degraded_from"] = degraded_from
    return data


def _degraded_provenance(state: AgentState) -> list[dict]:
    """降级**后仍拿到数据**时要交给答案模型的来源说明（T5 跟进 / spec §2「带说明的降级回答」）。

    P4-T5 真实模式抽查的实证缺陷：prompt「亚特兰蒂斯今天天气怎么样？」→ weather ``city_not_found``
    → 设计内的 search 兜底搜到【三亚亚特兰蒂斯水世界】→ 模型隐式换了实体，把水世界的天气当作
    “亚特兰蒂斯”的天气答出去（5 次 4 次）。兜底链本身没问题，缺陷在**答案不说明来源**：模型只
    看得到“一份成功的数据”，无从知道它是绕道来的，也就无从声明原始尝试失败过。

    所以把 ``degradation_log``（strategy/error/message，按 (strategy, error) 去重后）作为
    ``degraded_from`` 一起交给模型。全败路径**不交**：那条路径的 answer 分支自己逐条渲染
    ``result_data["degradation"]`` 说明原因（今天的 error 形状只有 ``all_strategies_failed``），
    再交一份就是把同一件事说两遍；而 ``result_data`` 里只要带 error，答案就不走 payload 这条路。
    """
    if not state.degradation_log:
        return []
    if state.result_data.get("error"):
        return []
    return _merge_degradation([], state.degradation_log)


def _merge_degradation(existing: list[dict], new: list[dict]) -> list[dict]:
    """合并降级记录：按 (strategy, error) 去重，保留首次出现的那条。

    resume 会重放 interrupt 之前的 execute 节点：重跑时 state 里已经带着上一轮
    的 degradation_log，直接相加会让同一条降级记录翻倍。去重键取 (strategy,
    error) —— 同一策略的不同失败原因仍然各自保留。返回新列表，不改动入参。
    """
    seen = {(e.get("strategy"), e.get("error")) for e in existing}
    merged = list(existing)
    for entry in new:
        key = (entry.get("strategy"), entry.get("error"))
        if key not in seen:
            merged.append(entry)
            seen.add(key)
    return merged


def _navigation_url(navigation: dict) -> str:
    """导航结果里的落点（最终 URL 优先，取不到就退到请求的 URL）。"""
    return str(navigation.get("url") or navigation.get("requested_url") or "")


def _observation_result(data: dict, navigation: dict | None) -> dict:
    """browser_task 的**观察收尾**（C2）：数据里标清本任务到底有没有导航过。

    没有导航就观察 = 看到的是"当时正好开着的页面"（上一次运行/别的会话留下的都可能）：
    它与用户目标的匹配关系从未被验证，所以标 ``partial`` —— 未与任务目标匹配的快照
    不得报 completed（否则 P4 拿到的是假成功 + 答非所问的答案）。
    """
    if navigation is None:
        return {
            **data,
            "navigated": False,
            "partial": True,
            "partial_reason": "no_navigation_for_task",
        }
    return {**data, "navigated": True, "opened_url": _navigation_url(navigation)}


def _navigation_only_result(navigation: dict) -> dict:
    """browser_task 的**导航收尾**（C2）：导航成功但没取到页面内容。

    数据只有"打开了哪个页面"——对"打开它并告诉我…"类任务而言，没看到内容就是没完成，
    所以标 ``partial``（不是失败：页面确实开了；但绝不能算 completed）。
    """
    return {
        **navigation,
        "navigated": True,
        "opened_url": _navigation_url(navigation),
        "partial": True,
        "partial_reason": "no_observation_after_navigation",
    }


def _partial_note(data: dict) -> str:
    """partial 结果 → 答案 prompt 里的一行（C2）；非 partial 时为空串。

    数据里的 ``partial_reason`` 是给机器看的稳定码，这行是给模型的指令：
    "完成了"不能用来说"只是看了一眼当时正好开着的页面"。
    """
    if not data.get("partial"):
        return ""
    return (
        "\n注意：数据里 partial=true —— 这次**没有完成与任务目标匹配的核查**"
        "（要么只观察了当时打开的页面，要么只打开了页面却没取到内容）。"
        "回答时明确说出已经看到什么、还有什么没完成，不要说任务已完成，也不要编造页面内容。"
    )


#: 降级成功路径的 system 规则（T5 跟进 / spec §2「带说明的降级回答」）。
#: 只在 payload 里真的有 ``degraded_from`` 时拼接（没降级就不提降级，不稀释正常回答）：
#: 数据面给了模型“哪些尝试失败过”，这条规则要求它把它说出口，而不是把兜底结果当原问题的答案。
_DEGRADED_ANSWER_RULE = (
    "注意：数据里的 degraded_from 是这份数据**降级前的失败尝试**（原始尝试的顺序、错误类型与原因）。"
    "出现 degraded_from 时必须：①先说明原始尝试失败了、失败原因是什么（用「未能找到…」「无法获取…」"
    "这类说法明确讲出来，不得略过）；②再说明现在这份数据来自哪条兜底路径（例如「未找到名为"
    "\"亚特兰蒂斯\"的城市天气，以下是与该名称相关的搜索结果：…」）；③不得把兜底结果当作原问题的"
    "答案直接陈述：不得用兜底结果里的实体替换原问题里的实体，也不得把兜底结果里该实体的具体数值"
    "（如温度）当作原问题的答案给出。"
)


#: 链上“成功后仍需继续”的步骤（C2）：``browser_open`` 只是**导航**，而浏览器任务的目标在页面上
#: ——“已打开 https://…”不等于任务完成。所以 open 成功后继续走 ``browser_snapshot``，观察结果才是
#: 任务的数据（导航信息并进 data）；观察失败时数据只到“打开了哪个页面”，结果按 partial 上报
#: （见 :func:`_observation_result` / :func:`_navigation_only_result`）。
_CONTINUE_AFTER: dict[str, str] = {"browser_open": "browser_snapshot"}


def _continues_to_observation(chain: list[str], index: int, tool_name: str) -> bool:
    """本步是链上“仍需继续”的**导航步**吗（C2 / F3 共用同一个判断，避免两处漂移）。

    ``True`` = “这一步本身只是导航，页面内容还在后面的观察步里”：成功分支靠它继续走观察步；
    HITL 分支靠它识别“暂停在导航步上 → resume 后没有观察”（F3：resume 走 hitl → answer，
    观察步永远不会跑，所以那时只有 open 元数据、没有页面内容，不得报 completed）。
    """
    follow_up = _CONTINUE_AFTER.get(tool_name)
    return follow_up is not None and follow_up in chain[index + 1 :]


def _hitl_result_data(
    data: dict, chain: list[str], index: int, tool_name: str
) -> dict:
    """HITL 暂停时的 ``result_data``（F3）：暂停在导航步上就标 ``partial``。

    链是 [browser_open, browser_snapshot] 时，暂停点通常是 open（登录墙在导航落地后就被检测到）：
    ``result_data`` 只有 open 元数据 + ``needs_human``，人工做完后 resume 直接进 answer，
    观察步**从未运行**——模型可能对从未读过的页面作答。所以标 partial + 稳定 reason
    （与 ``no_navigation_for_task`` / ``no_observation_after_navigation`` 同族），
    ``stop_reason`` 随之是 partial 而不是 completed。
    暂停在非导航步（如 tier1 的 weather 确认）时不标：那一步的结果就是任务的数据本身。
    """
    if not _continues_to_observation(chain, index, tool_name):
        return data
    return {**data, "partial": True, "partial_reason": "hitl_no_observation"}


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
    # read_page（tier2）HTTP 抓取失败 → 升级到真浏览器 browser_open（同一意图，降级链回退）；
    # browser_task（tier3）**先导航后观察**（C2）：任务里有 URL 就先去那个页面，再把页面内容取回来。
    #   旧链 [snapshot, open] 的两个真实性问题（P3 final review C2）：新任务必产一次假的
    #   no_open_page 降级（P4 会把每个 tier3 任务算一次失败）；有页面时停在 snapshot，
    #   而那个页面可能是上一次运行/别的会话留下的 —— 用别的页面的数据答本任务却报
    #   stop_reason=completed（假成功 + 答非所问）。
    # search 无有意义的回退，失败即明确降级（all_strategies_failed）。
    _STRATEGY_CHAIN: dict[str, list[str]] = {
        "weather": ["weather", "search"],
        "search": ["search"],
        "read_page": ["fetch_page", "browser_open"],
        "browser_task": ["browser_open", "browser_snapshot"],
    }

    # 工具 → 参数模型。``None`` = 工具不需要参数（不用花一次模型调用去提取）。
    # Tier3 的工具参数形态由 BrowserParams 描述（url/target/text/direction/expression），
    # 模型按需填；executor 每个工具只取自己要的字段。
    _PARAM_MODELS: dict[str, type[BaseModel] | None] = {
        "weather": WeatherParams,
        "search": SearchParams,
        "fetch_page": ReadPageParams,
        "browser_snapshot": None,
        "browser_open": BrowserParams,
        "browser_click": BrowserParams,
        "browser_type": BrowserParams,
        "browser_scroll": BrowserParams,
        "browser_execute_js": BrowserParams,
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
        结构化提取，直接用 task 原文作 query；无参数工具（browser_snapshot）同样跳过提取。
        全败 → result_data 返回 all_strategies_failed + degradation；全程 tool_call trace 可追溯。

        tier3（browser_task）的额外语义（C2，见 :func:`_observation_result`）：

        - ``browser_open`` 成功**不是终点**：继续走 chain 里的 ``browser_snapshot``
          （``_CONTINUE_AFTER``），因为任务目标在页面上，导航信息只是数据的一半。
        - 观察收尾必须能区分"本任务导航过"与"只是看了一眼当时开着的页面"：
          没导航过的快照标 ``partial``（不得报 completed）。
        - 导航成功但观察失败 → 标 ``partial``（打开了页面，但没取到内容）。
        - HITL 暂停在**导航步**上（F3，见 :func:`_hitl_result_data`）：resume 走 hitl → answer，
          观察步永远不会跑 → 标 ``partial``（``hitl_no_observation``），不得报 completed。
        """
        with tracer.span("execute", intent=state.intent):
            chain = _STRATEGY_CHAIN.get(state.intent, ["search"])
            degradation: list[dict] = []
            #: 本任务里成功导航的 open 结果 data（None = 没导航过）
            navigation: dict | None = None
            for index, tool_name in enumerate(chain):
                params_model = _PARAM_MODELS[tool_name]
                try:
                    if params_model is None:
                        # 无参数工具：结构化提取是空转，直接空参调用
                        params: BaseModel | None = None
                    elif tool_name == "search" and state.intent == "weather" and degradation:
                        # 天气城市未命中 → 用 task 原文搜索兑底，不再结构化提取
                        params = SearchParams(query=state.task)
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
                arguments = {} if params is None else params.model_dump()
                tool_result = await executor.call(tool_name, arguments)
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
                        # HITL：工具需要人工确认 → 交给 hitl 节点 interrupt（F3：暂停在导航步上时
                        # 标 partial —— resume 后走 hitl → answer，观察步永远不会跑）
                        return {
                            "result_data": _hitl_result_data(
                                tool_result.data, chain, index, tool_name
                            ),
                            "pending_human_action": True,
                            "human_prompt": str(tool_result.data.get("human_prompt", "")),
                            "degradation_log": _merge_degradation(
                                state.degradation_log, degradation
                            ),
                        }
                    if _continues_to_observation(chain, index, tool_name):
                        # 导航步成功：继续走观察步（导航信息随结果一起进 data）
                        navigation = dict(tool_result.data)
                        continue
                    result_data = tool_result.data
                    if state.intent == "browser_task":
                        result_data = (
                            _observation_result(tool_result.data, navigation)
                            if tool_name == "browser_snapshot"
                            else _navigation_only_result(tool_result.data)
                        )
                    return {
                        "result_data": result_data,
                        "stop_reason": "",
                        "degradation_log": _merge_degradation(
                            state.degradation_log, degradation
                        ),
                    }
                degradation.append(
                    {
                        "strategy": tool_name,
                        "error": tool_result.error_type,
                        "message": tool_result.error_message,
                    }
                )
            if navigation is not None:
                # 导航成功、观察步失败（C2）：结果仍是"已打开页面"，但**没有页面内容** ——
                # 标 partial 而不是 completed（任务目标在页面上，没看到内容就等于没完成）；
                # 观察步失败本身已经记在 degradation 里，不掩盖。
                return {
                    "result_data": _navigation_only_result(navigation),
                    "degradation_log": _merge_degradation(state.degradation_log, degradation),
                }
            return {
                "result_data": {"error": "all_strategies_failed", "degradation": degradation},
                "degradation_log": _merge_degradation(state.degradation_log, degradation),
            }

    async def answer(state: AgentState) -> dict:
        """合成最终回答；`stop_reason` 带出这次到底完成了没有（C2：partial 不得报 completed）。"""
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
                # C1：人工反馈进上下文（resume 后不读 human_response 等于把人工确认丢掉），
                # 并在 system 里把"人工已经做完"说清楚——否则模型会顺着数据里那句
                # "请手动登录"再要求人工操作一遍。
                # T5 跟进（spec §2「带说明的降级回答」）：payload 里出现 degraded_from（数据来自
                # 兜底路径）就拼上行为规则——“原始尝试失败了、为什么”“不得把兜底结果当作原问题
                # 的答案”。规则和数据面同时在场：只说其中一面，模型都可能跳过“没找到”这段。
                human_note = _human_feedback_note(state.human_response)
                payload = _answer_payload(state)
                system_content = "你是信息助手。用中文简洁回答，基于给定数据，不要编造。"
                if "degraded_from" in payload:
                    system_content += _DEGRADED_ANSWER_RULE
                if human_note:
                    system_content += (
                        "人工已经完成了先前需要人工处理的操作（登录/人机验证等）；"
                        "请基于人工反馈继续完成任务，不要再让用户手动登录或再次人工确认。"
                    )
                answer_result: ChatResult = await provider.chat(
                    [
                        Message(role="system", content=system_content),
                        Message(
                            role="user",
                            content=(
                                f"历史对话：\n{history_text}\n\n"
                                f"任务：{state.task}\n"
                                f"数据：{json.dumps(payload, ensure_ascii=False)}"
                                f"{_partial_note(state.result_data)}"
                                f"{human_note}"
                            ),
                        ),
                    ]
                )
                text = answer_result.text
            # C2：partial 是**第三种**结果（不是 completed、也不是 error）——
            # 只观察了未导航的页面 / 只打开了页面没看到内容，都必须如实标出来。
            stop_reason = "partial" if state.result_data.get("partial") else "completed"
            return {"final_answer": text, "stop_reason": stop_reason}

    async def hitl(state: AgentState) -> dict:
        """Human-in-the-loop：interrupt 暂停执行，resume 后带上人工确认回 answer。

        resume 只回到 answer：链上还没跑的步骤（典型的是导航步之后的观察步）**不会**自动继续
        —— 所以暂停在导航步上时，数据里只有 open 元数据，靠 ``partial`` 如实上报（F3，见
        :func:`_hitl_result_data`）；想拿到页面内容得由下一轮任务重新发起。
        """
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
