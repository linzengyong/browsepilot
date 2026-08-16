"""Intent routing for Tier1/2/3 — hybrid rules-first strategy (article 3).

Rules (keyword/regex) are cheap and deterministic; the small model is the
fallback for anything the rules don't recognize. Confidence thresholds are
deliberately deferred to P4 (tune against eval data, not gut feeling).
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

_URL_RE = re.compile(r"https?://[^\s，。！？\"]+")


class RouteDecision(BaseModel):
    intent: str  # weather | search | read_page | chitchat | knowledge_qa
    tier: str  # tier1 | tier2 | answer
    confidence: float = 1.0


# 规则顺序即优先级：先特判（天气/读网页/知识问答），后泛判（搜索/闲聊）
# 注意 knowledge_qa 必须在 search 之前（“什么是”同时匹配两者）
_RULES: list[tuple[str, str, list[str]]] = [
    ("weather", "tier1", ["天气", "气温", "温度", "多少度", "下雨", "下雪", "晴", "阴", "weather", "台风", "空气质量"]),
    ("read_page", "tier2", ["打开", "访问", "读", "阅读", "摘要"]),
    ("knowledge_qa", "answer", ["解释", "什么是", "定义", "区别", "原理", "如何理解"]),
    ("search", "tier1", ["搜索", "搜一下", "查一下", "帮我找", "search", "是什么", "什么时候", "为什么", "怎么样"]),
    ("chitchat", "answer", ["你好", "嗨", "hello", "hi", "谢谢", "再见", "你是谁"]),
]


class IntentResult(BaseModel):
    """用户任务意图分类结果。intent 取值：weather/search/read_page/chitchat/knowledge_qa；tier 取值：tier1/tier2/answer。"""
    intent: Literal["weather", "search", "read_page", "chitchat", "knowledge_qa"]
    tier: Literal["tier1", "tier2", "answer"]


def classify_by_rules(task: str) -> RouteDecision | None:
    text = task.strip().lower()
    if not text:
        return RouteDecision(intent="chitchat", tier="answer")
    if _URL_RE.search(task):
        return RouteDecision(intent="read_page", tier="tier2")
    for intent, tier, keywords in _RULES:
        if any(k.lower() in text for k in keywords):
            return RouteDecision(intent=intent, tier=tier)
    return None
