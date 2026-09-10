"""Structured parameter schemas for Tier1/2/3 tools (article 2 real-world use)."""

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


class BrowserParams(BaseModel):
    """Tier3 浏览器/交互任务参数（全部可选：模型按需填，各工具只取自己要的字段）。

    ``browser_open`` 用 ``url``；``browser_click``/``browser_type`` 用 ``target``；
    ``browser_type`` 还看 ``text``；``browser_scroll`` 看 ``direction``；
    ``browser_execute_js`` 看 ``expression``。``target`` 与工具参数名 ``ref``/``target``
    的别名由 executor 收敛（模型两种写法都能用）。``browser_snapshot`` 不需要参数，
    图的策略链对它不做结构化提取。
    """

    url: str = ""
    target: str = ""
    text: str = ""
    direction: str = "down"
    expression: str = ""
