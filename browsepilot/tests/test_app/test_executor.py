import pytest

from browsepilot.agent.tools import ToolExecutor, ToolSpec
from browsepilot.app.executor import AppToolExecutor
from browsepilot.app.services.weather import Weather


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
    assert names == {
        "weather",
        "search",
        "fetch_page",
        "browser_open",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_execute_js",
    }
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


@pytest.mark.asyncio
async def test_browser_tools_without_pool_are_structured_errors():
    """Tier3 是可选接线：未注入 BrowserPool 时浏览器工具必须明确降级（不抛异常、不假成功）。"""
    ex = AppToolExecutor(FakeWeather(), FakeSearch(), FakeFetch())
    for name in ("browser_open", "browser_snapshot", "browser_click"):
        result = await ex.call(name, {"url": "https://example.com/", "ref": "e2"})
        assert result.status == "error"
        assert result.error_type == "browser_unavailable"


@pytest.mark.asyncio
async def test_tier1_tools_still_work_with_browser_injected():
    """注入浏览器不改变 Tier1/2 工具的行为（接线是加法）。"""
    ex = AppToolExecutor(FakeWeather(), FakeSearch(), FakeFetch(), browser=object())
    result = await ex.call("weather", {"city": "北京"})
    assert result.status == "success"
    assert result.data["temperature_c"] == 25.0
