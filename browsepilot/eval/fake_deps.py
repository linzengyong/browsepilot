"""Fake pipeline dependencies shared by eval/run_eval.py (--mock) and tests.

Deterministic, offline, zero-token doubles of the ModelProvider and
ToolExecutor ports so the eval runner can execute scenarios in CI without
network access.

v0.4（P4-T2）：fake 从"固定罐头值"升级为**任务感知的确定性替身**。原因：场景化的内容断言
（t1 的城市名、t2-search 的结果链接、weather_not_found 的"明确失败而非编造"）如果没有真实
来源，mock 下的 PASS 就是假的——断言的可信度是评测的第一原则（P4 计划尾部）。规则很小、
可枚举，且与真实服务的**契约形状**一致（错误类型/字段名都对齐 AppToolExecutor）：

- ``FakeProvider.structured``：按 schema 字段名从**任务文本**里取参数
  （``"北京今天天气怎么样？"`` → city="北京"；url = 任务里的 URL 或域名；query = 去掉搜索动词的
  任务原文；classify 用的 IntentResult → ("search", "tier1")，与 graph 的兜底一致）。
- ``FakeProvider.chat``：按 answer 节点塞进 user message 的 ``数据：{json}`` 合成回答
  （搜索结果 → 标题 + URL + 摘要；天气 → 城市名 + 温度；页面 → 正文）；没有数据面（chitchat /
  knowledge_qa 直答路径）时退回固定罐头文本。"基于给定数据回答"是 answer 节点的语义，
  所以 fake 也必须这么做——否则 t1/t2 的内容断言在 mock 下没有真实来源。
- ``FakeExecutor.call``：
  - weather：已知城市（北京/上海）→ 成功；其余 → ``city_not_found``（WeatherService.resolve_city）
  - search：认识的查询（Python 发布时间）→ 带 URL/摘要的结果；其余 → ``service_error``
    （SearchService 无结果时 raise SearchError 的行为）
  - fetch_page：已知站点（python.org / example.com）→ 正文；其余 → ``service_error``
    （DNS/HTTP 失败时 FetchError 的行为）
  - 6 个浏览器工具 → ``browser_unavailable``：mock 环境**没有浏览器**（与
    ``AppToolExecutor(browser=None)`` 一致），tier3 任务必须明确降级（degrade 场景的靶子）
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel

from browsepilot.agent.tools import ToolResult, ToolSpec
from browsepilot.model.messages import ChatResult, Message

_CHAT_TEXT = "Python 3.13 于 2024 年发布"
"""无数据面（chitchat / knowledge_qa 直答）时的罐头回答（P0 起沿用）。"""

_FAKE_WEATHER: dict[str, float] = {"北京": 26.0, "上海": 24.0}
"""已知城市 → 温度；其余城市 → ``city_not_found``（resolve_city 的 CityNotFoundError）。"""

_FAKE_CONDITIONS = "晴"

_PYTHON_RESULTS: list[dict[str, str]] = [
    {
        "title": "Python 3.13 正式发布",
        "url": "https://www.python.org/downloads/release/python-3130/",
        "snippet": "Python 3.13 于 2024 年 10 月正式发布，官方文档与下载见 python.org。",
    },
    {
        "title": "Python 3.13 有哪些新特性",
        "url": "https://docs.python.org/3.13/whatsnew/3.13.html",
        "snippet": "Python 3.13 引入了新的交互式解释器、实验性自由线程等特性。",
    },
]
"""fake 的 search 只认识这一组结果（查询里含 "python"）；其余查询按 ddgs 无结果处理。"""

_FAKE_PAGES: dict[str, dict[str, str]] = {
    "python.org": {
        "url": "https://www.python.org",
        "title": "Welcome to Python.org",
        "text": "Python 是流行的编程语言：Python 3.13 于 2024 年发布，官网提供下载、文档与社区资源。",
    },
    "example.com": {
        "url": "https://example.com",
        "title": "Example Domain",
        "text": "Example Domain：This domain is for use in illustrative examples in documents.",
    },
}
"""fake 的 fetch_page 只认识这两个站点（按主机名索引，去 www. 前缀）；其余按 FetchError 处理。"""

_BROWSER_UNAVAILABLE_MSG = (
    "浏览器不可用：mock 环境没有浏览器（对应 AppToolExecutor(browser=None) 的 browser_unavailable）"
)

_BROWSER_TOOLS = frozenset(
    {
        "browser_open",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_execute_js",
    }
)

# ── 任务文本 → 结构化参数（fake 的"小模型"） ──

_WEATHER_TAIL_RE = re.compile(
    r"[，,。.！!？?]*\s*(?:的)?\s*(?:今天|明天|后天|现在|这两天)?\s*(?:的)?\s*"
    r"(?:天气|气温|温度|多少度|多少摄氏度).*$",
    re.S,
)
_SEARCH_VERB_RE = re.compile(r"^\s*(?:请|帮我)?\s*(?:搜索一下|搜一下|搜索|搜|查一下|查|找一下|找)\s*")
# 注意：排除集里**不能**含「.」—— 那会把 www.python.org 截成 www（T2 实测到的坑）。
_URL_RE = re.compile(r"https?://[^\s，、,。！？\"'）)】》]+")
_HOST_RE = re.compile(
    r"\b((?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s，、,。！？\"'）)】》]*)?)"
)
_DATA_LINE_RE = re.compile(r"^数据：(.*)$", re.M)


def _task_city(task: str) -> str:
    """任务里的城市名：摘掉天气问句的尾巴（"北京今天天气怎么样？" → "北京"）。"""
    return _WEATHER_TAIL_RE.sub("", task).strip(" ，,。.！!？?")


def _task_query(task: str) -> str:
    """任务的搜索 query：去掉"搜索一下"这类动词。"""
    return _SEARCH_VERB_RE.sub("", task).strip(" ，,。.！!？?")


def _task_url(task: str) -> str:
    """任务里的 URL；没有 scheme 的域名补上 https://（= 模型按 url 字段描述产出的形状）。"""
    match = _URL_RE.search(task)
    if match:
        return match.group(0)
    match = _HOST_RE.search(task)
    return f"https://{match.group(1)}" if match else ""


def _fake_params(schema: type[BaseModel], task: str) -> dict[str, Any]:
    """按 schema 字段名从任务文本取值（其余字段给空串）。"""
    values: dict[str, Any] = {}
    for field in schema.model_fields:
        if field == "city":
            values[field] = _task_city(task)
        elif field == "query":
            values[field] = _task_query(task)
        elif field == "url":
            values[field] = _task_url(task)
        elif field == "intent":
            values[field] = "search"
        elif field == "tier":
            values[field] = "tier1"
        else:
            values[field] = ""  # target/text/expression/…：交互链未入图，先给空串
    return values


def _host(url: str) -> str:
    """URL 的主机名（去 www. 前缀）——fake 的页面表按主机名索引。"""
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _compose_answer(messages: list[Message]) -> str:
    """按数据面合成回答（answer 节点的 user message 里有 ``数据：{json}``）。

    没有数据面（chitchat / knowledge_qa 直答路径）→ 固定罐头文本。
    """
    content = next((m.content for m in reversed(messages) if m.role == "user"), "")
    match = _DATA_LINE_RE.search(content)
    if match is None:
        return _CHAT_TEXT
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return _CHAT_TEXT
    if not isinstance(data, dict):
        return _CHAT_TEXT
    results = data.get("results")
    if isinstance(results, list) and results:
        top = results[0] if isinstance(results[0], dict) else {}
        return f"根据搜索结果：{top.get('title', '')}（{top.get('url', '')}）。{top.get('snippet', '')}"
    if data.get("city") and isinstance(data.get("temperature_c"), (int, float)):
        return (
            f"{data['city']}今天{data.get('conditions', '')}，"
            f"气温 {data['temperature_c']}°C。"
        )
    text = data.get("text")
    if isinstance(text, str) and text.strip():
        return f"页面主要内容：{text}"
    return _CHAT_TEXT


class FakeProvider:
    """ModelProvider double: task-aware structured params; answers synthesized from data."""

    async def chat(self, messages: list[Message], *, model: str | None = None) -> ChatResult:
        return ChatResult(text=_compose_answer(messages))

    async def stream(self, messages: list[Message], *, model: str | None = None):
        raise NotImplementedError("FakeProvider does not stream")

    async def structured(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel],
        model: str | None = None,
        max_retries: int = 3,
    ) -> BaseModel:
        task = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return schema(**_fake_params(schema, task))


class FakeExecutor:
    """ToolExecutor double: per-tool canned results shaped like ``AppToolExecutor``.

    刻意无状态（没有 ``__init__``）：测试里的 spy 子类可以随便怎么写构造器。
    """

    async def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="weather", description="fake weather", parameters={"city": "string"}),
            ToolSpec(name="search", description="fake search", parameters={"query": "string"}),
            ToolSpec(name="fetch_page", description="fake fetch", parameters={"url": "string"}),
            ToolSpec(
                name="browser_open", description="fake browser open", parameters={"url": "string"}
            ),
            ToolSpec(name="browser_snapshot", description="fake browser snapshot", parameters={}),
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "weather":
            return self._weather(arguments)
        if name == "search":
            return self._search(arguments)
        if name == "fetch_page":
            return self._fetch_page(arguments)
        if name in _BROWSER_TOOLS:
            return ToolResult(
                status="error",
                error_type="browser_unavailable",
                error_message=_BROWSER_UNAVAILABLE_MSG,
            )
        return ToolResult(
            status="error", error_type="unknown_tool", error_message=f"unknown tool: {name}"
        )

    @staticmethod
    def _weather(arguments: dict[str, Any]) -> ToolResult:
        city = str(arguments.get("city", "")).strip()
        for known, temperature in _FAKE_WEATHER.items():
            if known in city:  # resolve_city 是子串匹配（"北京市" → 北京）
                return ToolResult(
                    status="success",
                    content=f"{known}：{_FAKE_CONDITIONS}，{temperature}°C",
                    data={
                        "city": known,
                        "temperature_c": temperature,
                        "conditions": _FAKE_CONDITIONS,
                        "humidity": 40,
                        "wind_speed_kmh": 8.0,
                    },
                )
        return ToolResult(
            status="error",
            error_type="city_not_found",
            error_message=f"no coordinates for city: {city!r}",
        )

    @staticmethod
    def _search(arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query", ""))
        if "python" not in query.lower():
            return ToolResult(
                status="error",
                error_type="service_error",
                error_message=f"no results for query: {query!r}",
            )
        return ToolResult(
            status="success",
            content="\n".join(f"- {r['title']} {r['url']}" for r in _PYTHON_RESULTS[:5]),
            data={"results": [dict(r) for r in _PYTHON_RESULTS]},
        )

    @staticmethod
    def _fetch_page(arguments: dict[str, Any]) -> ToolResult:
        url = str(arguments.get("url", ""))
        page = _FAKE_PAGES.get(_host(url))
        if page is None:
            return ToolResult(
                status="error",
                error_type="service_error",
                error_message=(
                    f"fetch failed for {url}: no such host"
                    "（fake 只认识 python.org / example.com）"
                ),
            )
        return ToolResult(status="success", content=page["text"], data=dict(page))
