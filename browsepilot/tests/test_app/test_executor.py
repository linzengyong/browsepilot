import pytest

from browsepilot.agent.tools import ToolExecutor, ToolSpec
from browsepilot.app.executor import AppToolExecutor
from browsepilot.app.services.fetch import PageFetchService
from browsepilot.app.services.search import SearchService
from browsepilot.app.services.weather import Weather, WeatherService


class FakeWeather:
    async def get(self, city):
        return Weather(city=city, temperature_c=25.0, conditions="晴")


class FakeSearch:
    async def search(self, query, max_results=5):
        return []


class FakeFetch:
    async def fetch(self, url):
        return None


def test_app_tool_executor_implements_protocol():
    ex = AppToolExecutor(FakeWeather(), FakeSearch(), FakeFetch())
    assert isinstance(ex, ToolExecutor)


@pytest.mark.asyncio
async def test_list_tools_names():
    ex = AppToolExecutor(FakeWeather(), FakeSearch(), FakeFetch())
    tools = await ex.list_tools()
    names = {t.name for t in tools}
    assert names == {"weather", "search", "fetch_page"}
    assert all(isinstance(t, ToolSpec) for t in tools)


@pytest.mark.asyncio
async def test_call_weather():
    ex = AppToolExecutor(FakeWeather(), FakeSearch(), FakeFetch())
    result = await ex.call("weather", {"city": "北京"})
    assert result.status == "success"
    assert result.data["temperature_c"] == 25.0


@pytest.mark.asyncio
async def test_call_unknown_tool_returns_error():
    ex = AppToolExecutor(FakeWeather(), FakeSearch(), FakeFetch())
    result = await ex.call("nope", {})
    assert result.status == "error"
    assert result.error_type == "unknown_tool"
