"""Fake pipeline dependencies shared by eval/run_eval.py (--mock) and tests.

Deterministic, offline, zero-token doubles of the ModelProvider and
ToolExecutor ports so the eval runner can execute scenarios in CI without
network access. Values are chosen to satisfy the P0 scenario checks:

- FakeProvider.chat      -> ChatResult(text="Python 3.13 于 2024 年发布")
- FakeProvider.structured -> schema 字段映射：city=北京 / query=q / url=https://example.com
- FakeExecutor.call:
    weather    -> data={"city": "北京", "temperature_c": 26.0}
    search     -> data={"results": []}
    fetch_page -> data={"text": "Python 主页"}
    other      -> success, empty data
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from browsepilot.agent.tools import ToolResult, ToolSpec
from browsepilot.model.messages import ChatResult, Message

_CHAT_TEXT = "Python 3.13 于 2024 年发布"

# structured 参数提取：按 schema 字段名取值（与 P0 场景 checks 对齐）
_STRUCTURED_VALUES: dict[str, Any] = {
    "city": "北京",
    "query": "q",
    "url": "https://example.com",
}


class FakeProvider:
    """ModelProvider double: canned chat text; schema-driven structured values."""

    async def chat(self, messages: list[Message], *, model: str | None = None) -> ChatResult:
        return ChatResult(text=_CHAT_TEXT)

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
        data = {field: _STRUCTURED_VALUES.get(field, "") for field in schema.model_fields}
        return schema(**data)


class FakeExecutor:
    """ToolExecutor double: per-tool canned success results."""

    async def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="weather", description="fake weather", parameters={"city": "string"}),
            ToolSpec(name="search", description="fake search", parameters={"query": "string"}),
            ToolSpec(name="fetch_page", description="fake fetch", parameters={"url": "string"}),
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "weather":
            return ToolResult(status="success", content="北京：晴，26.0°C",
                              data={"city": "北京", "temperature_c": 26.0})
        if name == "search":
            return ToolResult(status="success", content="", data={"results": []})
        if name == "fetch_page":
            return ToolResult(status="success", content="Python 主页", data={"text": "Python 主页"})
        return ToolResult(status="success", content="", data={})
