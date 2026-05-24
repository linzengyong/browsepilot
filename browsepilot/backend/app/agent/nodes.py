"""LangGraph nodes: plan, execute, reflect, replan, answer."""

import json
import re
import os
import base64
from datetime import datetime, timezone

import jieba

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from backend.app.agent.state import AgentState
from backend.app.agent.prompts import (
    CLASSIFY_PROMPT,
    PLAN_SYSTEM_PROMPT,
    EXECUTE_SYSTEM_PROMPT,
    REFLECT_SYSTEM_PROMPT,
    COMPLETION_CHECK_PROMPT,
    REPLAN_PROMPT,
)
from backend.app.config import settings
from backend.app.agent.skill_loader import Skill
from langgraph.types import interrupt

USE_PLAYWRIGHT_MCP = os.getenv("USE_PLAYWRIGHT_MCP", "false").lower() == "true"


def extract_json(text: str) -> str | None:
    """Extract JSON string from LLM response. Handles markdown wrapping and extra text."""
    # 1. Try ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        return match.group(1).strip()
    # 2. Find first { to last } (JSON object — try before array since objects often contain arrays)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start and start < text.find("["):
        return text[start:end + 1]
    # 3. Find first [ to last ] (JSON array)
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    # 4. Fallback: { to } even if [ appears first
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return None


def repair_json(candidate: str) -> str:
    """Fix common JSON errors: trailing commas, single-quote keys/values."""
    # Remove trailing commas before } or ]
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    # Single-quoted keys: 'key': -> "key":
    candidate = re.sub(r"'([^']*)'\s*:", r'"\1":', candidate)
    # Single-quoted values: : 'value' -> : "value"
    candidate = re.sub(r":\s*'([^']*)'", r': "\1"', candidate)
    return candidate


# --- Tool scoring classifier ---

TOOL_RULES = {
    "browser_navigate": {
        "keywords": ["导航到", "访问", "打开", "跳转到"],
        "patterns": [r"https?://\S+", r"[a-z]+\.(com|cn|org)"],
    },
    "browser_snapshot": {
        "keywords": ["页面结构", "获取结构", "快照", "可用的元素", "当前页面状态", "页面内容", "获取内容", "提取"],
        "patterns": [r"获取.*(快照|结构|元素|内容)", r"当前.*页面"],
    },
    "browser_type": {
        "keywords": ["输入", "键入", "填写", "搜索"],
        "patterns": [r"在.{0,20}(输入|键入|填写|搜索)"],
    },
    "browser_click": {
        "keywords": ["点击", "按下", "提交"],
        "patterns": [r"点击.{0,20}(按钮|链接|搜索|提交)"],
    },
    "browser_wait_for": {
        "keywords": ["等待"],
        "patterns": [r"等待.{0,20}(加载|出现|渲染|完成)"],
    },
    "browser_take_screenshot": {
        "keywords": ["截图"],
        "patterns": [r"截图|screenshot"],
    },
    "browser_press_key": {
        "keywords": ["按键", "按下", "回车", "Enter", "滚动", "翻页"],
        "patterns": [r"(按下|按键|回车|Enter|滚动|翻页)"],
    },
    "browser_fill_form": {
        "keywords": ["填写表单", "表单", "注册", "登录表单"],
        "patterns": [r"填写.*表单|表单.*填写"],
    },
    "browser_evaluate": {
        "keywords": ["脚本", "执行脚本", "JavaScript"],
        "patterns": [r"执行.*(脚本|代码|JS)|JavaScript"],
    },
}

def classify_tool(step: str) -> str | None:
    """Score all tools against the step description. Returns tool name
    if best score >= threshold, else None (LLM fallback)."""
    best_tool = None
    best_score = 0
    for tool_name, rules in TOOL_RULES.items():
        score = 0
        for kw in rules["keywords"]:
            if kw in step:
                score += 1
        for pat in rules["patterns"]:
            if re.search(pat, step):
                score += 2
        if score > best_score:
            best_score = score
            best_tool = tool_name
    return best_tool if best_score >= settings.tool_classify_threshold else None


def extract_args(tool: str, step: str) -> tuple[dict | None, bool]:
    """Try to extract arguments for a tool from step description text.

    Returns (args_dict_or_None, is_complete).
    """
    if tool == "browser_navigate":
        m = re.search(r"https?://\S+", step)
        if m:
            return ({"url": m.group(0)}, True)
        m = re.search(r"(?:导航到|打开|访问|跳转到|去)\s*([a-z]+\.[a-z]+(?:\.[a-z]+)?)", step)
        if m:
            return ({"url": "https://" + m.group(1)}, True)
        return (None, False)

    if tool in ("browser_snapshot", "browser_take_screenshot"):
        return ({}, True)

    if tool in ("browser_click", "browser_type", "browser_wait_for",
                 "browser_press_key", "browser_fill_form", "browser_evaluate"):
        return (None, False)

    return (None, False)


def compute_plan_similarity(old_plan: list[str], new_plan: list[str]) -> float:
    """Jaccard similarity on word-level tokens using jieba."""
    def tokenize(steps):
        tokens = set()
        for s in steps:
            for word in jieba.lcut(s):
                word = word.strip()
                if word:
                    tokens.add(word)
        return tokens

    old_tokens = tokenize(old_plan)
    new_tokens = tokenize(new_plan)
    if not old_tokens or not new_tokens:
        return 0.0
    return len(old_tokens & new_tokens) / len(old_tokens | new_tokens)


async def parse_llm_json(
    llm,
    messages: list,
    node_name: str,
    fallback: dict,
    max_retries: int = 1,
) -> tuple[dict, dict | None]:
    """Unified LLM JSON invoke + parse + retry + fallback.

    Returns:
        (parsed_dict, token_usage_dict_or_None)
    """
    for attempt in range(max_retries + 1):
        try:
            response = await llm.ainvoke(messages)
            usage = response.usage_metadata
            text = response.content if hasattr(response, "content") else str(response)

            candidate = extract_json(text)
            if candidate is not None:
                try:
                    return json.loads(candidate), usage
                except json.JSONDecodeError:
                    repaired = repair_json(candidate)
                    try:
                        result = json.loads(repaired)
                        logger.info("[{}] JSON repaired successfully", node_name)
                        return result, usage
                    except json.JSONDecodeError as e:
                        logger.warning("[{}] JSON parse failed attempt {}: {}", node_name, attempt + 1, str(e)[:200])

            if attempt < max_retries:
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": "Your response format was incorrect. Return ONLY a JSON object, nothing else.",
                })
                continue

        except Exception as e:
            logger.error("[{}] LLM call failed: {}", node_name, str(e)[:200])
            raise

    logger.warning("[{}] JSON parse failed after {} retries, using fallback", node_name, max_retries)
    return fallback, None


def accumulate_tokens(current: dict, usage, node_name: str = "") -> dict:
    """Accumulate token usage with per-node breakdown."""
    if usage is None:
        return dict(current)
    # Handle both usage_metadata dict and AIMessage with usage_metadata attribute
    if hasattr(usage, 'usage_metadata'):
        usage = usage.usage_metadata
    if not usage:
        return dict(current)
    if "input_tokens" in usage:
        add_prompt = usage["input_tokens"]
    else:
        add_prompt = usage.get("prompt_tokens", 0)

    if "output_tokens" in usage:
        add_completion = usage["output_tokens"]
    else:
        add_completion = usage.get("completion_tokens", 0)
    return {
        "prompt": current.get("prompt", 0) + add_prompt,
        "completion": current.get("completion", 0) + add_completion,
        "breakdown": {
            **current.get("breakdown", {}),
            node_name: {"prompt": add_prompt, "completion": add_completion},
        },
    }


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: 2 chars per token."""
    return len(text) // 2


def get_llm():
    """Get the main (big) LLM instance for plan/execute/reflect/replan/answer."""
    return ChatOpenAI(
        model=settings.big_model,
        api_key=settings.big_model_api_key or settings.openai_api_key,
        base_url=settings.big_model_base_url or settings.openai_base_url,
    )


def get_small_llm():
    """Get the small LLM instance for intent classification."""
    return ChatOpenAI(
        model=settings.small_model,
        api_key=settings.small_model_api_key or settings.openai_api_key,
        base_url=settings.small_model_base_url or settings.openai_base_url,
    )


async def classify_node(state: AgentState) -> dict:
    """Classify user intent using the small model."""
    logger.info("[classify_node] Classifying: {}", state["task"][:80])
    llm = get_small_llm()

    result, usage = await parse_llm_json(
        llm=llm,
        messages=[
            SystemMessage(content=CLASSIFY_PROMPT),
            HumanMessage(content=state["task"]),
        ],
        node_name="classify",
        fallback={"intent": "browser_task"},
    )

    intent = result.get("intent", "browser_task")
    if intent not in ("chitchat", "knowledge_qa", "browser_task"):
        logger.warning("[classify_node] Unknown intent '{}', defaulting to browser_task", intent)
        intent = "browser_task"

    token_usage = accumulate_tokens(state.get("token_usage", {}), usage, "classify")

    return {
        "intent": intent,
        "token_usage": token_usage,
    }

async def pre_observe_node(state: AgentState, mcp_client) -> dict:
    """Navigate to target page and capture structure so plan_node knows the real DOM."""
    task = state["task"]
    url = settings.default_search_url

    # 1. Try direct URL match: https://xxx
    m = re.search(r"https?://\S+", task)
    if m:
        raw = m.group(0)
        url = re.sub(r"[，。！？；：\"\"''（）【】、《》…—]+$", "", raw)
    else:
        # 2. Try "去XXX上搜索/找/看" pattern
        m = re.search(r"去\s*(\S+?)\s*上", task)
        if m:
            site = m.group(1).strip()
            if "." in site:
                url = "https://" + site if not site.startswith("http") else site
            else:
                url = f"https://www.{site}.com"

    logger.info("[pre_observe] Navigating to {} for page observation", url)

    page_structure = {}
    page_screenshot = ""

    pre_meta = {"session_id": state.get("session_id", ""), "step_index": -1}
    try:
        nav_result = await mcp_client.call_tool("browser_navigate" if USE_PLAYWRIGHT_MCP else "navigate", {"url": url}, meta=pre_meta)
        if isinstance(nav_result, dict) and nav_result.get("status") == "error":
            logger.warning("[pre_observe] Navigate failed: {}", nav_result.get("error", ""))
    except Exception as e:
        logger.warning("[pre_observe] Navigate exception: {}", e)

    try:
        struct_result = await mcp_client.call_tool("browser_snapshot" if USE_PLAYWRIGHT_MCP else "get_page_structure", {}, meta=pre_meta)
        if isinstance(struct_result, dict):
            page_structure = struct_result
            if USE_PLAYWRIGHT_MCP:
                snap = struct_result.get("snapshot", "")
                ref_count = snap.count("[ref=e") if snap else 0
                logger.info("[pre_observe] Snapshot: {} refs", ref_count)
            else:
                s = struct_result.get("structure", struct_result)
                if isinstance(s, dict):
                    logger.info("[pre_observe] Structure: {} inputs, {} buttons, {} links",
                        len(s.get("inputs", [])), len(s.get("buttons", [])), len(s.get("links", [])))
    except Exception as e:
        logger.warning("[pre_observe] get_page_structure exception: {}", e)

    # Always capture screenshot for UI replay (not just LLM vision)
    screenshot_tool = "browser_take_screenshot" if USE_PLAYWRIGHT_MCP else "screenshot"
    try:
        img_result = await mcp_client.call_tool(screenshot_tool, {}, meta=pre_meta)
        if isinstance(img_result, dict):
            raw = img_result.get("raw_image") or img_result.get("data") or img_result.get("screenshot_base64")
            if raw:
                page_screenshot = raw if isinstance(raw, str) else base64.b64encode(raw).decode()
                logger.info("[pre_observe] Screenshot captured ({} chars)", len(page_screenshot))
    except Exception as e:
        logger.warning("[pre_observe] Screenshot exception: {}", e)

    return {
        "page_structure": page_structure,
        "page_screenshot": page_screenshot,
    }


async def plan_node(state: AgentState, mcp_client) -> dict:
    """Generate a structured execution plan from the user task."""
    logger.info("[plan_node] Generating plan for task: {}", state["task"][:80])
    llm = get_llm()

    tools_desc = "\n".join(
        f"- {t['name']}: {t.get('description', '')}"
        for t in mcp_client.tools
    )

    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system_prompt = f"Today's date is {current_date} (UTC).\n\n" + PLAN_SYSTEM_PROMPT.format(tools_desc=tools_desc)

    # Inject turn history context at the beginning for multi-turn memory
    turn_history = ""
    session_turns = state.get("session_turns", [])
    current_turn = state.get("turn_index", 0)
    past_turns = [t for t in session_turns if t.get("turn_index", 0) < current_turn]
    if past_turns:
        turn_history = "=== 对话历史 ===\n"
        turn_history += "以下是我们之前已经完成的对话轮次，请结合上下文理解当前任务：\n"
        for pt in past_turns[-3:]:
            turn_history += f"第{pt.get('turn_index', 0) + 1}轮 — 用户: {pt.get('task', '')}\n"
            answer = pt.get('final_answer', '')
            if answer:
                turn_history += f"回答摘要: {answer[:300]}\n\n"
        turn_history += "——\n\n"
        system_prompt = turn_history + system_prompt

    # Inject page structure context if available
    page_context = ""
    struct = state.get("page_structure", {})
    if isinstance(struct, dict):
        elements = struct.get("elements")
        if elements:
            page_context = "\n\n=== 当前页面可用元素（基于此生成计划） ===\n"
            page_context += "页面结构已获取，无需在计划中再次调用 get_page_structure。\n"
            page_context += f"元素列表: {json.dumps(elements, ensure_ascii=False)}\n"
        elif struct.get("structure"):
            # Legacy format (old session)
            s = struct["structure"]
            has_elements = s.get("inputs") or s.get("buttons") or s.get("links")
            if has_elements:
                page_context = "\n\n=== 当前页面真实结构（基于此生成计划，禁止编造选择器） ===\n"
                page_context += "页面结构已获取，无需在计划中再次调用 get_page_structure。\n"
                if s.get("inputs"):
                    page_context += f"输入框: {json.dumps(s['inputs'][:5], ensure_ascii=False)}\n"
                if s.get("buttons"):
                    page_context += f"按钮: {json.dumps(s['buttons'][:5], ensure_ascii=False)}\n"
                if s.get("links"):
                    page_context += f"链接: {json.dumps(s['links'][:5], ensure_ascii=False)}\n"

    system_prompt += page_context

    # Skill injection (progressive disclosure)
    system_prompt += "\n\n" + Skill.get_listing()
    matched = Skill.match(state["task"])
    if matched:
        system_prompt += "\n\n" + Skill.format_matched(matched)

    human_input = state["task"]
    human_content = human_input

    if settings.llm_vision_enabled and state.get("page_screenshot"):
        human_content = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{state['page_screenshot']}"}},
            {"type": "text", "text": human_input},
        ]

    plan, usage = await parse_llm_json(
        llm=llm,
        messages=[
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_content),
        ],
        node_name="plan",
        fallback=[state["task"], "回答用户"],
    )
    if not isinstance(plan, list):
        logger.warning("[plan_node] Plan is not a list, using fallback")
        plan = [state["task"], "回答用户"]

    token_usage = accumulate_tokens({}, usage, "plan")

    # Self-check: can this plan answer the user's question?
    self_check_prompt = f"""User task: {state["task"]}
Generated plan: {json.dumps(plan, ensure_ascii=False)}

After executing this plan, will the collected information be sufficient to answer the user's original question?
If not, append the missing steps to the end of the plan.
Return JSON: {{"sufficient": true/false, "extra_steps": []}}
Return ONLY JSON."""

    try:
        check_llm = get_llm()
        check_result, check_usage = await parse_llm_json(
            llm=check_llm,
            messages=[
                SystemMessage(content=self_check_prompt),
                HumanMessage(content="请检查计划是否充分"),
            ],
            node_name="plan_self_check",
            fallback={"sufficient": True, "extra_steps": []},
        )
        if not check_result.get("sufficient", True):
            extra = check_result.get("extra_steps", [])
            if extra:
                plan.extend(extra)
                logger.info("[plan_node] Self-check added {} extra steps", len(extra))
        if check_usage:
            token_usage = accumulate_tokens(token_usage, check_usage, "plan_self_check")
    except Exception as e:
        logger.warning("[plan_node] Self-check failed, using plan as-is: {}", e)

    return {
        "plan": plan,
        "retry_count": 0,
        "need_replan": False,
        "execution_log": state.get("execution_log") or [],
        "token_usage": token_usage,
        "plan_step_count": 0,
    }


async def execute_node(state: AgentState, mcp_client, tools: list) -> dict:
    """Execute the first step in the plan using the appropriate MCP tool."""
    if not state["plan"]:
        logger.info("[execute_node] No steps remaining in plan")
        return {}

    current_step = state["plan"][0]
    logger.info("[execute_node] Executing step: {}", current_step)

    llm = get_llm()

    # Build tool descriptions for LLM tool selection (tools are raw MCP dicts)
    tools_desc = "\n".join(
        f"- {t['name']}: {t.get('description', '')}"
        for t in tools
    )

    # Build context from recent execution results (especially get_page_structure / browser_snapshot output)
    recent_context = ""
    for i, entry in enumerate(state["execution_log"]):
        result = entry.get("result", {})
        if isinstance(result, dict) and result.get("snapshot"):
            recent_context += "\n页面元素引用（来自 browser_snapshot）：\n"
            recent_context += result["snapshot"]
            recent_context += "\n"
        elif isinstance(result, dict) and result.get("elements"):
            # New label format (legacy get_page_structure)
            recent_context += f"\n页面可用元素（来自之前的get_page_structure）：\n"
            recent_context += f"元素列表: {json.dumps(result['elements'], ensure_ascii=False)}\n"
            if result.get("summary"):
                recent_context += f"摘要: {result['summary']}\n"
        elif isinstance(result, dict) and result.get("structure"):
            # Legacy format (old session with "structure" field)
            s = result["structure"]
            recent_context += f"\n页面可用元素（旧格式）：\n"
            if s.get("inputs"):
                recent_context += "输入框: " + json.dumps(s["inputs"][:10], ensure_ascii=False) + "\n"
            if s.get("buttons"):
                recent_context += "按钮: " + json.dumps(s["buttons"][:10], ensure_ascii=False) + "\n"
            if s.get("links"):
                recent_context += "链接: " + json.dumps(s["links"][:5], ensure_ascii=False) + "\n"
        elif isinstance(result, dict) and result.get("content"):
            recent_context += f"\n页面文本摘要（前500字）：{result['content'][:500]}\n"

    # Append tool call history so LLM knows what's been done
    if state["execution_log"]:
        recent_context += "\nPreviously executed tools:\n"
        for i, entry in enumerate(state["execution_log"][-5:]):
            tool = entry.get("tool", "unknown")
            status = "success" if entry.get("result", {}).get("status") != "error" else "failed"
            step_desc = entry.get("step", "")[:60]
            recent_context += f"{i+1}. {tool} ({status}) — {step_desc}\n"

    prompt = EXECUTE_SYSTEM_PROMPT.format(
        tools_desc=tools_desc,
        recent_context=recent_context,
    )

    # Scoring classifier: try to skip LLM for obvious tool+arg combinations
    classified_tool = classify_tool(current_step)
    usage = None
    if classified_tool:
        logger.info("[execute_node] Classifier selected: {} (score >= {})", classified_tool, settings.tool_classify_threshold)
        extracted_args, is_complete = extract_args(classified_tool, current_step)

        if extracted_args is not None and is_complete:
            # All args extracted from step text — skip LLM entirely
            tool_selection = {"tool": classified_tool, "arguments": extracted_args, "step": current_step}
            logger.info("[execute_node] Args extracted, skipping LLM: {}", extracted_args)
        else:
            # Partial or no args — LLM fills remaining
            arg_prompt = prompt + (
                f"\n\nThe tool is '{classified_tool}'. "
                "Only return the arguments for this tool. "
                "Return JSON: {\"arguments\": {...}, \"step\": \"...\"}"
            )
            if extracted_args:
                arg_prompt += f"\nAlready known arguments (do not change): {json.dumps(extracted_args, ensure_ascii=False)}"
            try:
                llm_result, usage = await parse_llm_json(
                    llm=llm,
                    messages=[SystemMessage(content=arg_prompt), HumanMessage(content=current_step)],
                    node_name="execute",
                    fallback=None,
                    max_retries=0,
                )
                if llm_result and isinstance(llm_result.get("arguments"), dict):
                    if extracted_args:
                        llm_result["arguments"] = {**extracted_args, **llm_result["arguments"]}
                    merged_args = llm_result.get("arguments", {})
                    # Validate: tools that require args must get non-empty args
                    REQUIRES_ARGS = {"browser_click", "browser_type", "browser_navigate", "browser_evaluate"}
                    if classified_tool in REQUIRES_ARGS and not merged_args:
                        raise ValueError(f"{classified_tool} requires arguments but LLM returned empty dict")
                    tool_selection = {"tool": classified_tool, "arguments": merged_args, "step": llm_result.get("step", current_step)}
                else:
                    raise ValueError("LLM returned no valid arguments")
            except Exception:
                logger.warning("[execute_node] Classifier+LLM path failed, falling through to full LLM selection")
                classified_tool = None  # trigger full LLM path below

    if not classified_tool:
        # Path C: full LLM tool selection with bind_tools() for native function calling
        llm_with_tools = llm.bind_tools(tools)

        try:
            response = await llm_with_tools.ainvoke([
                SystemMessage(content=prompt),
                HumanMessage(content=current_step),
            ])

            if hasattr(response, "tool_calls") and response.tool_calls:
                tc = response.tool_calls[0]
                tool_selection = {
                    "tool": tc["name"],
                    "arguments": tc.get("args", {}),
                    "step": current_step,
                }
                usage = response.usage_metadata
            else:
                # No tool_calls — fall back to text-based parsing
                raise ValueError("No tool_calls in response")
        except Exception:
            logger.warning("[execute_node] bind_tools path failed, using text fallback")
            tool_selection, usage = await parse_llm_json(
                llm=llm,
                messages=[SystemMessage(content=prompt), HumanMessage(content=current_step)],
                node_name="execute",
                fallback={"tool": "browser_snapshot" if USE_PLAYWRIGHT_MCP else "get_content", "arguments": {}, "step": current_step},
            )

    token_usage = accumulate_tokens(state.get("token_usage", {}), usage, "execute")

    result = {}
    tool_used = tool_selection.get("tool", "none") if isinstance(tool_selection, dict) else "none"

    if tool_used and tool_used != "none":
        arguments = tool_selection.get("arguments", {}) if isinstance(tool_selection, dict) else {}
        logger.info("[execute_node] Calling tool {} with args {}", tool_used, arguments)
        try:
            result = await mcp_client.call_tool(
                tool_used, arguments,
                meta={"session_id": state.get("session_id", ""), "step_index": state.get("total_steps", 0)},
            )
        except Exception as e:
            logger.warning("[execute_node] Tool call failed: {}", e)
            result = {"status": "error", "error": f"tool_call_failed: {str(e)}"}

        # Auto-capture screenshot after page-modifying operations
        if isinstance(result, dict) and result.get("status") != "error":
            page_modifying = USE_PLAYWRIGHT_MCP and tool_used in (
                "browser_navigate", "browser_click", "browser_type", "browser_press_key",
            )
            if not USE_PLAYWRIGHT_MCP:
                page_modifying = tool_used in ("navigate", "click", "type_text", "scroll")
            if page_modifying:
                try:
                    ss_tool = "browser_take_screenshot" if USE_PLAYWRIGHT_MCP else "screenshot"
                    ss_result = await mcp_client.call_tool(ss_tool, {}, timeout=10.0)
                    if isinstance(ss_result, dict) and ss_result.get("screenshot_base64"):
                        result["screenshot_base64"] = ss_result["screenshot_base64"]
                except Exception:
                    pass
    else:
        result = {"status": "skipped", "reason": "no_tool_needed"}

    # Use screenshot path from tool result (tool already saved to disk)
    screenshot_path = result.get("screenshot_path", "") if isinstance(result, dict) else ""

    new_log = state["execution_log"] + [{
        "step": current_step,
        "tool": tool_used,
        "result": result,
        "screenshot_path": screenshot_path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "retry_count": state["retry_count"],
    }]

    success = isinstance(result, dict) and result.get("status") != "error"

    if success:
        new_plan = state["plan"][1:]   # Success: advance to next step
        retry_count = 0
    else:
        new_plan = state["plan"]       # Failure: keep current step for retry
        retry_count = state.get("retry_count", 0) + 1

    return {
        "execution_log": new_log,
        "plan": new_plan,
        "retry_count": retry_count,
        "total_steps": state.get("total_steps", 0) + 1,
        "plan_step_count": state.get("plan_step_count", 0) + 1,
        "token_usage": token_usage,
        "messages": state["messages"] + [
            HumanMessage(content=f"步骤: {current_step}"),
            HumanMessage(content=f"结果: {json.dumps(result, ensure_ascii=False)[:500]}"),
        ],
    }


# --- Reflect node: two-level reflection ---

def _run_heuristic_checks(state: AgentState) -> dict:
    """Code-level heuristic checks on the last execution step. Zero LLM cost.

    Returns dict with keys: has_issue, issue_type, detail
    """
    if not state.get("execution_log"):
        return {"has_issue": False, "issue_type": None, "detail": ""}

    last_entry = state["execution_log"][-1]
    last_result = last_entry.get("result", {})
    if not isinstance(last_result, dict):
        return {"has_issue": False, "issue_type": None, "detail": ""}

    # Check 1: Page content too short — only for get_content tool
    if last_entry.get("tool") == ("browser_snapshot" if USE_PLAYWRIGHT_MCP else "get_content"):
        content_text = last_result.get("result", last_result.get("content", ""))
        if isinstance(content_text, str) and content_text.strip():
            # Strip HTML tags for meaningful text length check
            clean = re.sub(r"<[^>]+>", "", content_text).strip()
            if len(clean) < 50:
                return {
                    "has_issue": True,
                    "issue_type": "content_short",
                    "detail": f"Page content only {len(clean)} meaningful characters",
                }

    # Check 2: URL domain check — compare navigate result with expected domain
    step = last_entry.get("step", "")
    last_url = last_result.get("url", "")
    if last_url and step:
        # Extract expected domain from step description
        url_match = re.search(r"https?://([^/\s]+)", step)
        if url_match:
            expected_domain = url_match.group(1)
            actual_domain = re.sub(r"^https?://", "", last_url).split("/")[0]
            if expected_domain not in actual_domain and actual_domain not in expected_domain:
                return {
                    "has_issue": True,
                    "issue_type": "domain_mutation",
                    "detail": f"Expected {expected_domain}, got {actual_domain}",
                }

    # Check 3: Consecutive similar results (dead loop detection)
    recent = state["execution_log"][-3:]
    if len(recent) >= 3:
        texts = []
        for entry in recent:
            r = entry.get("result", {})
            if isinstance(r, dict):
                t = r.get("result", r.get("content", ""))
                texts.append(str(t)[:200] if isinstance(t, str) else "")
        if all(texts) and len(set(texts)) <= 1:
            return {
                "has_issue": True,
                "issue_type": "similar_results",
                "detail": "Last 3 steps produced identical results",
            }

    # Check 4: No interactive elements (page likely broken or blank)
    elements = last_result.get("elements")
    if isinstance(elements, dict):
        if len(elements) == 0 and ("browser_snapshot" if USE_PLAYWRIGHT_MCP else "get_page_structure") in last_entry.get("tool", ""):
            return {
                "has_issue": True,
                "issue_type": "few_elements",
                "detail": "No interactive elements found — page may be blank or broken",
            }
    elif last_result.get("structure") is not None:
        structure = last_result["structure"]
        if isinstance(structure, dict):
            input_count = len(structure.get("inputs", []))
            button_count = len(structure.get("buttons", []))
            if input_count == 0 and button_count == 0 and ("browser_snapshot" if USE_PLAYWRIGHT_MCP else "get_page_structure") in last_entry.get("tool", ""):
                return {
                    "has_issue": True,
                    "issue_type": "few_elements",
                    "detail": "No interactive elements found — page may be blank or broken",
                }

    # Check 5: Click that should trigger navigation but URL didn't change
    if last_entry.get("tool") == ("browser_click" if USE_PLAYWRIGHT_MCP else "click") and last_entry.get("result", {}).get("status") == "success":
        step_text = last_entry.get("step", "")
        nav_keywords = ("搜索", "提交", "跳转", "登录", "注册", "登录", "确认", "submit", "login", "search", "进入", "下一页")
        if any(kw in step_text for kw in nav_keywords):
            url_before = last_result.get("url_before", "")
            url_after = last_result.get("url_after", "")
            if url_before and url_after and url_before == url_after:
                return {
                    "has_issue": True,
                    "issue_type": "click_no_navigation",
                    "detail": "Clicked element expected to navigate, but URL did not change",
                }

    # Check 6: MCP validation error wrapped as success result
    result_text = last_result.get("result", "")
    if isinstance(result_text, str) and result_text.startswith("Error executing tool"):
        return {
            "has_issue": True,
            "issue_type": "mcp_validation_error",
            "detail": result_text[:200],
        }

    return {"has_issue": False, "issue_type": None, "detail": ""}


def _last_step_failed(state: AgentState) -> bool:
    """Check if the last execution step resulted in an error."""
    if not state.get("execution_log"):
        return False
    last = state["execution_log"][-1]
    result = last.get("result", {})
    return isinstance(result, dict) and result.get("status") == "error"


async def _completion_check(state: AgentState) -> dict:
    """Check if collected info is sufficient to answer user's question."""
    logger.info("[reflect_node] Running completion check...")

    # Build execution summary
    execution_summary = "\n".join(
        f"- {e.get('step', '')}: {str(e.get('result', {}).get('status', 'unknown'))}"
        for e in state.get("execution_log", [])[-10:]
    )

    # Extract page contents
    page_contents = []
    for e in state.get("execution_log", []):
        result = e.get("result", {})
        if isinstance(result, dict):
            content = result.get("result", result.get("content", ""))
            if isinstance(content, str) and len(content) > 20:
                page_contents.append(content[:500])

    context = COMPLETION_CHECK_PROMPT.format(
        task=state["task"],
        execution_summary=execution_summary,
        page_contents_summary="\n---\n".join(page_contents[-5:]) or "(none)",
    )

    llm = get_llm()
    check_result, usage = await parse_llm_json(
        llm=llm,
        messages=[
            SystemMessage(content=context),
            HumanMessage(content="请判断信息是否足够回答用户问题"),
        ],
        node_name="reflect_completion",
        fallback={"action": "answer"},
    )

    action = check_result.get("action", "answer")
    extra_steps = check_result.get("extra_steps", [])

    result = {
        "completion_check_count": state.get("completion_check_count", 0) + 1,
    }

    if usage:
        result["token_usage"] = accumulate_tokens(state.get("token_usage", {}), usage, "reflect")

    if action == "continue" and extra_steps:
        logger.info("[reflect_node] Info insufficient, adding {} extra steps", len(extra_steps))
        result["plan"] = extra_steps
        result["need_replan"] = False
    else:
        logger.info("[reflect_node] Info sufficient, routing to answer")
        result["need_replan"] = False

    return result


async def _llm_reflection(state: AgentState, heuristic_result: dict | None = None) -> dict:
    """LLM-based deep reflection on step failure or heuristic issues."""
    logger.info("[reflect_node] Running LLM deep reflection...")

    # Build context
    context = f"User task: {state['task']}\n\n"

    if state.get("execution_log"):
        last = state["execution_log"][-1]
        context += f"Last step: {last.get('step', '')}\n"
        context += f"Result: {json.dumps(last.get('result', {}), ensure_ascii=False)[:500]}\n"

    if heuristic_result and heuristic_result.get("has_issue"):
        context += f"\nHeuristic check found issue: {heuristic_result.get('issue_type')} — {heuristic_result.get('detail')}\n"

    if state.get("stagnation_warning"):
        context += """\n\n⚠ IMPORTANT WARNING: The previous replan produced a plan highly similar to the old one (>80% similarity).
Current strategy may be in a dead loop. You MUST try a fundamentally different approach:
- Change the order of operations
- Try a different navigation path
- Fall back to a search engine if the current page cannot complete the task
If no viable alternative exists, select answer."""

    context += f"\n\nCurrent remaining plan: {json.dumps(state.get('plan', []), ensure_ascii=False)}\n"

    messages = [
        SystemMessage(content=REFLECT_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ]

    llm = get_llm()
    reflection, usage = await parse_llm_json(
        llm=llm,
        messages=messages,
        node_name="reflect_llm",
        fallback={"action": "answer", "reason": "reflection failed, defaulting to answer"},
    )

    action = reflection.get("action", "answer")
    logger.info("[reflect_node] LLM reflection decision: {} — {}", action, reflection.get("reason", ""))

    result = {}
    if usage:
        result["token_usage"] = accumulate_tokens(state.get("token_usage", {}), usage, "reflect")

    if action == "retry":
        if state.get("retry_count", 0) >= 2:
            # Too many retries — force replan
            result["need_replan"] = True
        else:
            result["need_replan"] = False
    elif action == "replan":
        result["need_replan"] = True
    else:
        result["need_replan"] = False  # answer

    return result


_HITL_HEADLESS_STOP = {
    "plan": [], "need_replan": False,
    "stop_reason": "检测到需要登录验证的网站。当前运行在 headless 模式下，无法进行人工干预。请在 .env 中设置 BROWSER_HEADLESS=false 后重试。"
}


def _hitl_navigate_hit(state: AgentState) -> dict | None:
    """Check if last navigate hit a login/captcha wall."""
    last_entry = state["execution_log"][-1] if state.get("execution_log") else None
    if not last_entry:
        return None
    if last_entry.get("tool") != ("browser_navigate" if USE_PLAYWRIGHT_MCP else "navigate"):
        return None
    result = last_entry.get("result", {})
    issues = result.get("detected_issues", []) if isinstance(result, dict) else []
    if not any(issue in issues for issue in ["login_wall", "captcha"]):
        return None
    if settings.browser_headless:
        return _HITL_HEADLESS_STOP
    logger.info("[reflect_node] HITL triggered: detected {}", issues)
    return {
        "plan": [], "need_replan": False,
        "pending_human_action": True,
        "human_prompt": "检测到登录验证页面。请在浏览器中手动完成登录后，点击'继续'按钮恢复任务。",
        "hitl_triggered_count": state.get("hitl_triggered_count", 0) + 1,
    }


def _hitl_verify_redirect(state: AgentState) -> dict | None:
    """Check for repeated verify/captcha/challenge URL redirects."""
    last_entry = state["execution_log"][-1] if state.get("execution_log") else None
    if not last_entry:
        return None
    tool = last_entry.get("tool")
    nav_tool = "browser_navigate" if USE_PLAYWRIGHT_MCP else "navigate"
    click_tool = "browser_click" if USE_PLAYWRIGHT_MCP else "click"
    if tool not in (nav_tool, click_tool):
        return None
    url = last_entry.get("result", {}).get("url_after", "")
    if not isinstance(url, str) or not any(kw in url.lower() for kw in ["verify", "captcha", "challenge", "login"]):
        return None
    verify_count = sum(
        1 for e in state["execution_log"]
        if isinstance(e.get("result", {}).get("url_after", ""), str)
        and any(kw in e["result"]["url_after"].lower() for kw in ["verify", "captcha", "challenge"])
    )
    if verify_count < 2:
        return None
    if settings.browser_headless:
        return _HITL_HEADLESS_STOP
    logger.info("[reflect_node] HITL triggered: repeated verify URL ({})", verify_count)
    return {
        "plan": [], "need_replan": False,
        "pending_human_action": True,
        "human_prompt": "连续跳转到验证页面，怀疑需要人工完成验证。请在浏览器中处理后点击'继续'。",
        "hitl_triggered_count": state.get("hitl_triggered_count", 0) + 1,
    }


async def reflect_node(state: AgentState) -> dict:
    """Analyze the last execution result and decide next action (two-level reflection)."""
    logger.info("[reflect_node] Reflecting on execution...")

    # Circuit breaker checks — force answer with reason
    if state.get("consecutive_failures", 0) >= settings.consecutive_failures_threshold:
        logger.warning("[reflect_node] Circuit breaker: {} consecutive failures", state["consecutive_failures"])
        return {"plan": [], "need_replan": False,
                "stop_reason": f"连续 {state['consecutive_failures']} 步执行失败，为避免无限重试，按照现有搜集到的材料组织回答"}
    if state.get("replan_count", 0) >= settings.replan_max_count:
        logger.warning("[reflect_node] Too many replans: {}", state["replan_count"])
        return {"plan": [], "need_replan": False,
                "stop_reason": f"已进行 {state['replan_count']} 次重新规划，当前任务可能无法通过浏览器自动化完成，按照现有搜集到的材料组织回答"}
    if state.get("stagnation_count", 0) >= settings.stagnation_threshold:
        logger.warning("[reflect_node] Stagnation detected: {}", state["stagnation_count"])
        return {"plan": [], "need_replan": False,
                "stop_reason": "连续多步结果高度相似，判断陷入死循环，按照现有搜集到的材料组织回答"}
    plan_steps = state.get("plan_step_count", 0)
    total_steps_all = state.get("total_steps", len(state.get("execution_log", [])))
    if plan_steps >= settings.plan_step_limit:
        logger.warning("[reflect_node] Plan step limit reached: {} steps in current plan", plan_steps)
        return {"plan": [], "need_replan": False,
                "stop_reason": f"当前计划已执行 {plan_steps} 步，达到单轮上限，按照现有搜集到的材料组织回答"}
    if total_steps_all >= settings.total_step_limit:
        logger.warning("[reflect_node] Total step limit reached: {} total steps", total_steps_all)
        return {"plan": [], "need_replan": False,
                "stop_reason": f"总计已执行 {total_steps_all} 步，达到全局上限，按照现有搜集到的材料组织回答"}

    # --- HITL detection ---
    if state.get("hitl_triggered_count", 0) == 0:
        hitl_result = _hitl_navigate_hit(state) or _hitl_verify_redirect(state)
        if hitl_result:
            return hitl_result

    # Level 1: Heuristic check on last step (code-level, zero LLM cost)
    heuristic_result = _run_heuristic_checks(state)
    if heuristic_result.get("has_issue"):
        logger.info("[reflect_node] Heuristic check found issue: {} — {}",
                    heuristic_result.get("issue_type"), heuristic_result.get("detail"))

    # Level 1 continued: check if step failed
    last_step_failed_result = _last_step_failed(state)

    # Level 2: Deep reflection triggers
    has_plan = state.get("plan") and len(state["plan"]) > 0

    if not has_plan:
        # Plan is empty — completion check
        completion_check_count = state.get("completion_check_count", 0)
        if completion_check_count >= 1:
            # Already checked once, don't loop
            logger.info("[reflect_node] Completion check already done, routing to answer")
            return {"need_replan": False}

        return await _completion_check(state)

    if not last_step_failed_result and (not heuristic_result or not heuristic_result.get("has_issue")):
        # Step succeeded and no heuristic issues — continue
        logger.info("[reflect_node] Step OK, continuing execution")
        return {"need_replan": False}

    # Level 2: LLM deep reflection
    return await _llm_reflection(state, heuristic_result)


# --- Replan node with vision context ---


def _build_replan_context(state: AgentState):
    """Build replan context, optionally with vision-enabled screenshots.

    Content fields (page HTML/text) are truncated to avoid token explosion.
    """
    text = f"Original task: {state['task']}\n\n"
    text += "Execution log:\n"

    # Build truncated execution log — strip large content fields
    truncated_log = []
    for entry in state.get("execution_log", [])[-5:]:
        e = dict(entry)
        result = e.get("result", {})
        if isinstance(result, dict):
            r = dict(result)
            # Truncate page content and screenshot data to avoid token explosion
            for key in ("result", "content", "screenshot_base64"):
                if key in r and isinstance(r[key], str):
                    r[key] = r[key][:500] + "..." if len(r[key]) > 500 else r[key]
            # Truncate structure arrays to avoid massive element lists in replan
            if "structure" in r and isinstance(r["structure"], dict):
                for field in ("inputs", "buttons", "links"):
                    if field in r["structure"] and isinstance(r["structure"][field], list):
                        r["structure"][field] = r["structure"][field][:3]
            # Truncate elements dict to avoid large label lists in replan
            if "elements" in r and isinstance(r["elements"], dict):
                truncated = {}
                for k, v in list(r["elements"].items())[:10]:
                    truncated[k] = v
                r["elements"] = truncated
            e["result"] = r
        truncated_log.append(e)
    text += f"{json.dumps(truncated_log, ensure_ascii=False, indent=2)}\n"
    text += f"\nCurrent plan (failed): {json.dumps(state.get('plan', []), ensure_ascii=False)}\n"
    text += f"\nRetry count: {state.get('retry_count', 0)}\n"

    if not settings.llm_vision_enabled:
        return text  # Plain text only

    # Build multimodal content with screenshots
    content: list = [{"type": "text", "text": text}]

    for entry in state.get("execution_log", [])[-3:]:
        result = entry.get("result", {})
        if isinstance(result, dict) and result.get("status") == "error":
            screenshot_path = entry.get("screenshot_path", "")
            if screenshot_path and os.path.exists(screenshot_path):
                try:
                    with open(screenshot_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    })
                    logger.info("[replan_node] Added screenshot for vision analysis")
                except Exception as e:
                    logger.warning("[replan_node] Failed to encode screenshot: {}", e)

    return content


async def replan_node(state: AgentState, mcp_client) -> dict:
    """Generate a new plan based on what has been done and what failed."""
    logger.info("[replan_node] Regenerating plan...")
    llm = get_llm()

    tools_desc = "\n".join(
        f"- {t['name']}: {t.get('description', '')}"
        for t in mcp_client.tools
    )

    # Build context (with or without vision)
    context_content = _build_replan_context(state)

    replan_prompt = REPLAN_PROMPT.format(tools_desc=tools_desc)

    messages = [
        SystemMessage(content=replan_prompt),
        HumanMessage(content=context_content),
    ]

    new_plan, usage = await parse_llm_json(
        llm=llm,
        messages=messages,
        node_name="replan",
        fallback=["回答用户（基于已完成步骤给出部分结果）"],
    )
    if not isinstance(new_plan, list):
        logger.warning("[replan_node] Replan result is not a list, using fallback")
        new_plan = ["回答用户（基于已完成步骤给出部分结果）"]

    token_usage = accumulate_tokens(state.get("token_usage", {}), usage, "replan")

    similarity = compute_plan_similarity(state.get("plan", []), new_plan)

    if similarity == 1.0:
        logger.warning("[replan_node] Identical plan generated, giving up")
        return {
            "plan": state.get("plan", []),
            "need_replan": False,
            "stagnation_count": state.get("stagnation_count", 0) + 1,
            "replan_count": state.get("replan_count", 0) + 1,
            "token_usage": token_usage,
            "plan_step_count": 0,
        }

    if similarity > 0.8:
        logger.warning("[replan_node] Highly similar plan (sim={:.2f})", similarity)
        return {
            "plan": new_plan,
            "stagnation_count": state.get("stagnation_count", 0) + 1,
            "stagnation_warning": True,
            "replan_count": state.get("replan_count", 0) + 1,
            "need_replan": False,
            "token_usage": token_usage,
            "plan_step_count": 0,
        }

    return {
        "plan": new_plan,
        "stagnation_count": 0,
        "stagnation_warning": False,
        "replan_count": state.get("replan_count", 0) + 1,
        "need_replan": False,
        "token_usage": token_usage,
        "plan_step_count": 0,
    }


# --- Answer node with dual-path fallback ---


def compress_execution_log(execution_log: list, max_tokens: int = 4000) -> str:
    """Compress execution log: last 3 steps full detail, older steps summary only."""
    if len(execution_log) <= 3:
        parts = []
        for e in execution_log:
            result_str = json.dumps(e.get("result", {}), ensure_ascii=False)[:200]
            parts.append(f"- {e.get('step', '')}: {result_str}")
        return "\n".join(parts)

    recent = execution_log[-3:]
    older = execution_log[:-3]
    parts = ["## Earlier steps (summary)"]
    for e in older:
        status = e.get("result", {}).get("status", "unknown") if isinstance(e.get("result"), dict) else "unknown"
        parts.append(f"- {e.get('step', '')} [{status}]")
    parts.append("\n## Recent steps (detailed)")
    for e in recent:
        result_str = json.dumps(e.get("result", {}), ensure_ascii=False)[:300]
        parts.append(f"- {e.get('step', '')}: {result_str}")
    return "\n".join(parts)


def build_context_with_budget(
    system_prompt: str,
    task: str,
    messages: list,
    execution_log: list,
    page_contents: list[str],
    max_tokens: int = 8000,
) -> str:
    """Assemble context by priority; truncate low-priority content when over budget."""
    core = f"{system_prompt}\n\nUser task: {task}"
    budget = max_tokens - estimate_tokens(core)
    if budget <= 0:
        return core[: max_tokens * 2]

    # Execution log (up to 50% of remaining budget, max 4000)
    log_budget = min(int(budget * 0.5), 4000)
    log_text = compress_execution_log(execution_log, max_tokens=log_budget)
    budget -= estimate_tokens(log_text)

    # Page contents (up to 30% of remaining budget)
    content_budget = int(budget * 0.3)
    content_parts = []
    for pc in reversed(page_contents):
        chunk = pc[:content_budget * 2]
        if not chunk:
            break
        content_parts.insert(0, chunk)
    content_text = "\n---\n".join(content_parts) if content_parts else ""
    budget -= estimate_tokens(content_text)

    # Messages (remaining budget)
    msg_parts = []
    max_msgs = getattr(settings, 'max_messages_count', 50)
    for msg in reversed(messages[-max_msgs:]):
        msg_content = getattr(msg, "content", str(msg))
        if isinstance(msg_content, list):
            msg_content = str(msg_content[0]) if msg_content else ""
        chunk = msg_content[: max(budget * 2, 200)]
        if not chunk:
            break
        msg_parts.insert(0, chunk)

    parts = [core, log_text]
    if content_text:
        parts.append(f"Page contents:\n{content_text}")
    return "\n\n".join(parts)


def _extract_page_contents(execution_log: list) -> list[str]:
    """Extract page content strings from execution log for answer context."""
    contents = []
    for entry in execution_log:
        result = entry.get("result", {})
        if isinstance(result, dict):
            text = result.get("result", result.get("content", ""))
            if isinstance(text, str) and len(text) > 20:
                contents.append(text[:1000])
    return contents


async def answer_node(state: AgentState) -> dict:
    """Prepare answer context. If HITL is pending, interrupts execution."""
    # HITL: pause execution for human intervention
    if state.get("pending_human_action"):
        logger.info("[answer_node] HITL: interrupting for human action")
        interrupt(state["human_prompt"])
        logger.info("[answer_node] HITL: resumed after human action")
        return {
            "pending_human_action": False,
            "human_prompt": "",
        }

    logger.info("[answer_node] Preparing answer context...")

    if not state.get("execution_log"):
        # No execution happened: chitchat or knowledge_qa direct path
        recent_messages = (state.get("messages") or [])[-10:]
        answer_messages = [
            SystemMessage(content=f"今天是 {datetime.now(timezone.utc).strftime('%Y-%m-%d')}（UTC）。你是 BrowsePilot，一个擅长浏览器自动化的 AI 助手。请直接简洁地回答用户的问题。"),
            *recent_messages,
            HumanMessage(content=state["task"]),
        ]
    else:
        # browser_task path: use compressed execution context
        page_contents = _extract_page_contents(state["execution_log"])

        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        system_prompt = (
            f"今天是 {current_date}（UTC）。"
            "你是 BrowsePilot，一个浏览器自动化助手。请根据下面的浏览器执行结果回答用户的问题。"
        )
        stop_reason = state.get("stop_reason", "")
        if stop_reason:
            system_prompt += (
                f"\n\n注意：本次任务因以下原因被中断，未完整执行所有步骤——{stop_reason}。"
                "请在回答开头向用户说明此情况（用自然的语言描述原因），"
                "然后根据已收集到的信息尽最大努力回答用户的问题。"
            )

        context = build_context_with_budget(
            system_prompt=system_prompt,
            task=state["task"],
            messages=state.get("messages", []),
            execution_log=state["execution_log"],
            page_contents=page_contents,
        )
        answer_messages = [SystemMessage(content=context)]

    # Store prepared messages — main.py will stream the LLM response
    return {
        "answer_messages": answer_messages,
    }


def get_answer_messages(state: AgentState) -> list:
    """Get the prepared answer messages from state for streaming in main.py."""
    return state.get("answer_messages", [])
