"""Eval scenario schema and loader (article 4 material)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Check(BaseModel):
    """声明式断言（v0.3，P4-D1 全类型；语义见 ``eval/checks.py``）。

    - ``field``：``field`` + ``expected``（``kind: number`` → 只断言"是数值"）
    - ``contains_keyword`` / ``not_contains``：``keywords``
    - ``tools_called``：``tools``（``keywords`` 是同字段别名）+ ``mode``
      （``contains`` 默认 | ``not_contains`` | ``exact_sequence``）
    - ``tier_is`` / ``stop_reason_is``：``expected``
    - ``tokens_below``：``limit``；``latency_below``：``limit_ms``
    - ``degradation_absent`` / ``needs_human``：无参数
    """

    # 类型集合（P4-D1）：field | contains_keyword | not_contains | tools_called | tier_is |
    # stop_reason_is | tokens_below | latency_below | degradation_absent | needs_human
    type: str
    field: str | None = None
    expected: str | None = None
    kind: str | None = None  # number | string
    keywords: list[str] | None = None
    tools: list[str] | None = None  # tools_called 的期望工具名
    mode: str | None = None  # tools_called: contains(默认) | not_contains | exact_sequence
    limit: int | None = None  # tokens_below 的 token 预算
    limit_ms: float | None = None  # latency_below 的毫秒预算


class Scenario(BaseModel):
    id: str
    name: str
    prompt: str
    tier: str = "tier1"
    checks: list[Check] = Field(default_factory=list)


def load_scenarios(directory: Path) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        scenarios.append(Scenario(**data))
    return scenarios
