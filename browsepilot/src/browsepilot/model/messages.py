"""Unified message / usage / cost models (model layer, pi-ai equivalent).

Owned by the model layer. Nothing above this layer may depend on a
provider-specific format (OpenAI/Anthropic/...) directly.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Usage(BaseModel):
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    total_tokens: int = 0


class Cost(BaseModel):
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


class UsageInfo(BaseModel):
    usage: Usage = Field(default_factory=Usage)
    cost: Cost = Field(default_factory=Cost)
    estimated: bool = False


class Message(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str = ""
    tool_call_id: str | None = None
    name: str | None = None

    def to_openai(self) -> dict:
        out: dict = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            out["name"] = self.name
        return out


class ChatResult(BaseModel):
    text: str = ""
    usage: UsageInfo = Field(default_factory=UsageInfo)
    raw: dict = Field(default_factory=dict)
