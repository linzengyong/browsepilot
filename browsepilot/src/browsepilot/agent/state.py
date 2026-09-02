"""Agent state models (Pydantic v2). Replaces the flat TypedDict of v1."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExecutionStep(BaseModel):
    index: int = 0
    tool: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    status: str = "running"  # running | success | error
    error_message: str = ""
    duration_ms: float | None = None
    timestamp: str = ""


class AgentState(BaseModel):
    task: str = ""
    session_id: str = ""
    history: list[str] = Field(
        default_factory=list
    )  # 历史摘要："Q: … / A: …"，最多 5 条、截断 ~200 字符
    intent: str = ""  # browser_task | chitchat | knowledge_qa
    tier: str = ""  # tier1 | tier2 | tier3
    plan: list[str] = Field(default_factory=list)
    execution_log: list[ExecutionStep] = Field(default_factory=list)
    final_answer: str = ""
    total_steps: int = 0
    consecutive_failures: int = 0
    replan_count: int = 0
    stop_reason: str = ""
    result_data: dict[str, Any] = Field(
        default_factory=dict
    )  # structured payload from the last tool call
    degradation_log: list[dict[str, Any]] = Field(
        default_factory=list
    )  # 策略链每次失败的降级记录：{"strategy", "error", "message"}
    pending_human_action: bool = False  # HITL：工具返回 needs_human 后置位
    human_prompt: str = ""  # 待人工确认的问题（interrupt 载荷）
    human_response: str = ""  # resume 后人工确认的内容
    hitl_triggered_count: int = 0  # 本线程已触发 interrupt 的次数（防循环）
