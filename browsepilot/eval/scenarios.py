"""Eval scenario schema and loader (article 4 material)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Check(BaseModel):
    type: str  # field | contains_keyword
    field: str | None = None
    expected: str | None = None
    kind: str | None = None  # number | string
    keywords: list[str] | None = None


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
