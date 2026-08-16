import httpx
import pytest

from browsepilot.app.services.city_coords import CITY_COORDS
from browsepilot.app.services.weather import (
    WMO_TO_CONDITIONS,
    CityNotFoundError,
    WeatherService,
)


def test_city_table_has_major_cities():
    for c in ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安", "南京", "重庆"]:
        assert c in CITY_COORDS
    assert CITY_COORDS["北京"][0] == pytest.approx(39.9042, abs=0.01)


def test_city_table_has_aliases():
    assert CITY_COORDS["北京市"] == CITY_COORDS["北京"]
    assert CITY_COORDS["beijing"] == CITY_COORDS["北京"]


def test_wmo_code_mapping_spot_checks():
    assert WMO_TO_CONDITIONS[0] == "晴"
    assert WMO_TO_CONDITIONS[61] == "小雨"
    assert WMO_TO_CONDITIONS[95] == "雷暴"


def test_resolve_city_exact_match():
    from browsepilot.app.services.weather import resolve_city

    name, lat, lon = resolve_city("北京")
    assert name == "北京"
    assert lat == pytest.approx(39.9042, abs=0.01)


def test_resolve_city_substring_match():
    from browsepilot.app.services.weather import resolve_city

    name, _, _ = resolve_city("北京市海淀区")
    assert name == "北京"


def test_resolve_city_unknown_raises():
    from browsepilot.app.services.weather import resolve_city

    with pytest.raises(CityNotFoundError):
        resolve_city("不存在的城市XYZ")


@pytest.mark.asyncio
async def test_weather_service_get():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["latitude"] == "39.9042"
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 26.5,
                    "relative_humidity_2m": 60,
                    "weather_code": 0,
                    "wind_speed_10m": 12.3,
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = WeatherService(client=client)
    weather = await service.get("北京")
    assert weather.city == "北京"
    assert weather.temperature_c == 26.5
    assert weather.conditions == "晴"
    assert weather.humidity == 60
