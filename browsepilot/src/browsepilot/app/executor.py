"""App-layer ToolExecutor: wires agent tool calls to the Tier1/2 services."""
from __future__ import annotations

from typing import Any

from browsepilot.agent.tools import ToolResult, ToolSpec
from browsepilot.app.services.fetch import FetchError, PageFetchService
from browsepilot.app.services.search import SearchError, SearchService
from browsepilot.app.services.weather import CityNotFoundError, WeatherService

_TOOL_SPECS = [
    ToolSpec(name="weather", description="查询指定中国城市的当前天气（温度/天气状况/湿度/风速）",
             parameters={"city": "string"}),
    ToolSpec(name="search", description="搜索互联网获取信息",
             parameters={"query": "string"}),
    ToolSpec(name="fetch_page", description="抓取并提取指定 URL 的网页正文",
             parameters={"url": "string"}),
]


class AppToolExecutor:
    def __init__(
        self,
        weather: WeatherService,
        search: SearchService,
        fetch: PageFetchService,
    ):
        self._weather = weather
        self._search = search
        self._fetch = fetch

    async def list_tools(self) -> list[ToolSpec]:
        return _TOOL_SPECS

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            if name == "weather":
                w = await self._weather.get(arguments["city"])
                return ToolResult(status="success", content=f"{w.city}：{w.conditions}，{w.temperature_c}°C",
                                  data=w.model_dump())
            if name == "search":
                results = await self._search.search(arguments["query"])
                return ToolResult(
                    status="success",
                    content="\n".join(f"- {r.title} {r.url}" for r in results[:5]),
                    data={"results": [r.model_dump() for r in results]},
                )
            if name == "fetch_page":
                page = await self._fetch.fetch(arguments["url"])
                return ToolResult(status="success", content=page.text, data=page.model_dump())
            return ToolResult(status="error", error_type="unknown_tool", error_message=f"unknown tool: {name}")
        except CityNotFoundError as exc:
            return ToolResult(status="error", error_type="city_not_found", error_message=str(exc))
        except (SearchError, FetchError) as exc:
            return ToolResult(status="error", error_type="service_error", error_message=str(exc))
