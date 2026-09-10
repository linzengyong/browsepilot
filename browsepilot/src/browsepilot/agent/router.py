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
    intent: str  # weather | search | read_page | browser_task | chitchat | knowledge_qa
    tier: str  # tier1 | tier2 | tier3 | answer
    confidence: float = 1.0


# 规则顺序即优先级：先特判（天气/读网页），再知识问答，再交互/垂类（browser_task），
# 后泛判（搜索/闲聊）。
# 注意 knowledge_qa 必须在 search 之前（“什么是”同时匹配两者）。
# browser_task 的位置也是规则：排在 read_page 之后（“打开/访问/阅读”仍归 tier2，不抢）、
# knowledge_qa 之后（“什么是表单登录”这类教科书提问仍归 knowledge_qa）、search 之前
# ——带站点/交互标记的任务（比价/购物/京东/点击…）比泛化的“搜索一下”更具体，
# 具体规则优先。
#
# browser_task 的判定不是单纯的关键词表（强词直接命中；弱词还要页面上下文共现），
# 所以它的 keyword 位是 ``None``，由 :func:`_is_browser_task` 在循环里显式求值；
# 占位位置仍然表达优先级。
_RULES: list[tuple[str, str, list[str] | None]] = [
    (
        "weather",
        "tier1",
        [
            "天气",
            "气温",
            "温度",
            "多少度",
            "下雨",
            "下雪",
            "晴",
            "阴",
            "weather",
            "台风",
            "空气质量",
        ],
    ),
    ("read_page", "tier2", ["打开", "访问", "读", "阅读", "摘要"]),
    ("knowledge_qa", "answer", ["解释", "什么是", "定义", "区别", "原理", "如何理解"]),
    ("browser_task", "tier3", None),
    (
        "search",
        "tier1",
        ["搜索", "搜一下", "查一下", "帮我找", "search", "是什么", "什么时候", "为什么", "怎么样"],
    ),
    ("chitchat", "answer", ["你好", "嗨", "hello", "hi", "谢谢", "再见", "你是谁"]),
]

# ── browser_task 的两段式触发（I1：泛化弱动词不得抢 search 的既有匹配）─────────────
# 评审探针：“为什么选择 Python 而不是 Java” / “查一下怎么提交表单” / “选择哪个框架比较好” /
# “Python 输入输出教程” / “git 提交规范” 都曾被单字动词（选择/提交/输入）抢成 tier3，
# 而 tier3 链（snapshot → open）对这些问题没有 search 兜底 → all_strategies_failed。

_SITE_WORDS: tuple[str, ...] = (
    "比价",
    "购物",
    "下单",
    "加购",
    "加入购物车",
    "购物车",
    "结算",
    "商城",
    "京东",
    "淘宝",
    "天猫",
    "拼多多",
)
"""电商/站点词（含技能触发词）：本身就是“去这个网站办事”的信号。"""

_BROWSER_STRONG_KEYWORDS: tuple[str, ...] = (*_SITE_WORDS, "搜索框", "浏览器")
"""强触发词：站点/电商词 + 明确的页面控件词——单独出现即可判 tier3（无需上下文）。"""

_BROWSER_WEAK_KEYWORDS: tuple[str, ...] = (
    "点击",
    "填写",
    "输入",
    "提交",
    "勾选",
    "选择",
    "登录",
    "注册",
    "表单",
)
"""弱触发词：日常提问里也常见的泛化交互动词/名词，必须与页面上下文共现才算 tier3。"""

_BROWSER_CONTEXT_RE = re.compile(
    r"页面|按钮|表单|框(?!架)|网站|" + "|".join(map(re.escape, _SITE_WORDS))
)
"""页面上下文标记：弱动词旁边有“页面上的东西”才算交互任务（“点击登录按钮”）。

裸“框”算（输入框/弹框/搜索框…），**“框架”不算**——“选择哪个框架比较好”是技术选型提问（I1 反例）。
"""

_BROWSER_INFO_SEEKING_RE = re.compile(r"怎么|如何|为什么|教程|规范|指南|入门")
"""求知/教程措辞：问“怎么做”而不是“去做”（“查一下怎么提交表单”），弱动词在此不成 tier3。

强触发词（京东/比价…）不受此限：那是“在某个网站上办事”的明确信号。
"""


def _is_browser_task(text: str) -> bool:
    """browser_task 命中判定（text 已小写）：强词直接算；弱词要页面上下文且不是求知提问。"""
    if any(k in text for k in _BROWSER_STRONG_KEYWORDS):
        return True
    if _BROWSER_INFO_SEEKING_RE.search(text):
        return False
    return any(k in text for k in _BROWSER_WEAK_KEYWORDS) and bool(
        _BROWSER_CONTEXT_RE.search(text)
    )


class IntentResult(BaseModel):
    """用户任务意图分类结果。

    intent 取值：weather/search/read_page/browser_task/chitchat/knowledge_qa；
    tier 取值：tier1/tier2/tier3/answer。
    """

    intent: Literal["weather", "search", "read_page", "browser_task", "chitchat", "knowledge_qa"]
    tier: Literal["tier1", "tier2", "tier3", "answer"]


def classify_by_rules(task: str) -> RouteDecision | None:
    text = task.strip().lower()
    if not text:
        return RouteDecision(intent="chitchat", tier="answer")
    if _URL_RE.search(task):
        return RouteDecision(intent="read_page", tier="tier2")
    for intent, tier, keywords in _RULES:
        if keywords is None:  # browser_task：强词/弱词两套判定（见 _is_browser_task）
            if _is_browser_task(text):
                return RouteDecision(intent=intent, tier=tier)
            continue
        if any(k.lower() in text for k in keywords):
            return RouteDecision(intent=intent, tier=tier)
    return None
