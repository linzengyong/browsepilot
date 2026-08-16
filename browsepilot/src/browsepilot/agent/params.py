"""Structured parameter schemas for Tier1/2 tools (article 2 real-world use)."""
from __future__ import annotations

from pydantic import BaseModel


class WeatherParams(BaseModel):
    """天气查询参数。city 为中文城市名，如 北京、上海。"""
    city: str


class SearchParams(BaseModel):
    """搜索查询参数。query 为完整搜索语句。"""
    query: str


class ReadPageParams(BaseModel):
    """网页读取参数。url 为完整 http(s) 链接。"""
    url: str
