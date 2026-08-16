"""Tier1 weather service: built-in city table + Open-Meteo (no API key)."""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from browsepilot.app.services.city_coords import CITY_COORDS


class CityNotFoundError(ValueError):
    pass


# WMO weather codes -> simplified Chinese conditions (article 2/3 material)
WMO_TO_CONDITIONS: dict[int, str] = {
    0: "晴",
    1: "多云",
    2: "阴",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "毛毛雨",
    56: "冻雨",
    57: "冻雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴冰雹",
    99: "强雷暴伴冰雹",
}


class Weather(BaseModel):
    city: str
    temperature_c: float
    conditions: str
    humidity: int | None = None
    wind_speed_kmh: float | None = None


def resolve_city(city: str) -> tuple[str, float, float]:
    """Longest-match lookup: exact -> suffix/pinyin -> substring. Returns (canonical, lat, lon)."""
    stripped = city.strip().lower()
    if stripped in CITY_COORDS:
        return city.strip(), *CITY_COORDS[stripped]
    for key, coords in CITY_COORDS.items():
        if key in stripped and key not in ("市",):  # 防止单字误命中
            return key, *coords
    raise CityNotFoundError(f"no coordinates for city: {city!r}")


class WeatherService:
    BASE_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    async def get(self, city: str) -> Weather:
        name, lat, lon = resolve_city(city)
        client = self._client or httpx.AsyncClient()
        try:
            resp = await client.get(
                self.BASE_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                },
                timeout=10,
            )
            resp.raise_for_status()
            current = resp.json()["current"]
            return Weather(
                city=name,
                temperature_c=current["temperature_2m"],
                conditions=WMO_TO_CONDITIONS.get(current["weather_code"], "未知"),
                humidity=current.get("relative_humidity_2m"),
                wind_speed_kmh=current.get("wind_speed_10m"),
            )
        finally:
            if self._client is None:
                await client.aclose()
