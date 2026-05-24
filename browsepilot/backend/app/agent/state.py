"""AgentState definition for the BrowsePilot LangGraph agent."""

from typing import Annotated, TypedDict
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    task: str
    session_id: str  # for per-session screenshot directories
    intent: str
    plan: list[str]
    execution_log: list[dict]
    degradation_log: list[dict]  # [{"node": "plan", "reason": "json_parse_failed", "timestamp": "..."}]
    retry_count: int
    need_replan: bool
    final_answer: str
    token_usage: dict  # {"prompt": 0, "completion": 0, "breakdown": {"classify": {...}, "plan": {...}, ...}}
    total_steps: int  # Global step counter (never reset), upper limit 30
    plan_step_count: int  # Steps within current plan (reset on replan), upper limit 15
    completion_check_count: int  # max 1 completion check per session
    consecutive_failures: int  # consecutive step failures
    stagnation_count: int  # stagnation detection counter
    replan_count: int  # number of replans in this session
    stagnation_warning: bool  # flag to inject warning into reflect prompt
    stop_reason: str  # reason for forced stop (circuit breaker, step limit, etc.)
    answer_messages: list  # prepared messages for streaming answer generation
    page_structure: dict
    page_screenshot: str
    turn_index: int
    session_turns: list[dict]  # past turn summaries, injected by main.py, not persisted
    pending_human_action: bool         # 是否需要人工干预
    human_prompt: str                   # 显示给用户的提示信息
    hitl_triggered_count: int          # 已触发 HITL 次数（resume 后不清零，防止重复触发）
