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
    intent: str = ""  # browser_task | chitchat | knowledge_qa
    tier: str = ""  # tier1 | tier2 | tier3
    plan: list[str] = Field(default_factory=list)
    execution_log: list[ExecutionStep] = Field(default_factory=list)
    final_answer: str = ""
    total_steps: int = 0
    consecutive_failures: int = 0
    replan_count: int = 0
    stop_reason: str = ""
