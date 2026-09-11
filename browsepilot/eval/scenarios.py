"""Eval scenario schema and loader (article 4 material).

Schema v2（P4-T2）把场景从"prompt + checks"扩成**声明式评测单元**，字段分三类：

- 描述：``id`` / ``name`` / ``prompt`` / ``tier``（路由的**实际**层级）/ ``checks``
- 运行选择：``mode``（这条场景在哪个模式下有意义）/ ``enabled`` / ``tags``
- 运行行为：``timeout_seconds`` / ``fragile`` / ``follow_up``

选择语义（:mod:`eval.run_eval` 实现，schema 只负责"声明得对不对"）：

- ``mode`` = ``mock`` | ``real`` | ``both``。mock 的 FakeProvider/FakeExecutor 只伪造
  天气/搜索/抓页（**没有浏览器**），所以 tier3/HITL 场景在 mock 下是"跑不了"（SKIP、
  不计失败、不进 exit code），而不是"跑失败了"——把环境不匹配读成失败会污染成功率。
- ``fragile``：断言依赖外部动态内容（ddgs 结果漂移）。失败照实打 FAIL，但**不进 exit code**，
  报告里单独计数（P4-D6）。
- ``enabled=False``：声明了但暂不跑的场景（P3 遗留的交互链、未接线的多轮 harness）。
- ``follow_up``：多轮场景第 2..n 轮的 prompt（harness 接线在 T3/T5，本轮先落 schema）。
- ``tags``：P4-D2 的 ``KNOWN_TAGS``；缺省 = ``acceptance``。

校验原则是 **fail-loud**：未知 tag / 未知 mode / 未知字段 / 空 checks / 空 prompt /
重复 id 都是**加载错误**——一次笔误不该静默变成"少跑一条场景"或"少断言一条"
（评测的价值在于断言的可信度）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

KNOWN_TAGS: tuple[str, ...] = ("smoke", "acceptance", "browser", "hitl", "degrade")
"""P4-D2 的 tag 集合。

- ``smoke``：CI 必跑的确定性核心（mock 下可复现、不依赖外部内容漂移）
- ``acceptance``：阶段验收全量（缺省值）
- ``browser``：需要真浏览器
- ``hitl``：需要真浏览器 + 人工介入（headful）
- ``degrade``：降级行为（工具不可用时的诚实失败）
"""

ScenarioMode = Literal["mock", "real", "both"]


class Check(BaseModel):
    """声明式断言（v0.3，P4-D1 全类型；语义见 ``eval/checks.py``）。

    - ``field``：``field`` + ``expected``（``kind: number`` → 只断言"是数值"）
    - ``contains_keyword`` / ``not_contains``：``keywords``
    - ``tools_called``：``tools``（``keywords`` 是同字段别名）+ ``mode``
      （``contains`` 默认 | ``not_contains`` | ``exact_sequence``）
    - ``tier_is`` / ``stop_reason_is``：``expected``
    - ``tokens_below``：``limit``；``latency_below``：``limit_ms``
    - ``degradation_absent`` / ``needs_human``：无参数

    ``extra="forbid"``（P4-T2 审查 I1）：``keyword:``（漏 s）这类笔误必须报错——
    静默忽略 = 少断言一条，而日志上看不出来（与 :class:`Scenario` 同一条原则）。
    """

    model_config = ConfigDict(extra="forbid")

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
    """一个场景 = 一次 ``run_task`` + 一组断言（+ 运行选择元数据）。

    ``extra="forbid"``：YAML 里的字段名笔误（``mdoe: real``）必须报错——静默忽略会让
    场景以错误的 mode/enabled 被跑，而日志上看不出来。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    prompt: str
    # 路由的**实际**层级（search 规则 → tier1，即便文件叫 t2_*）；拼错（tier4）在加载时报错
    tier: Literal["tier1", "tier2", "tier3"] = "tier1"
    tags: list[str] = Field(default_factory=lambda: ["acceptance"])
    mode: ScenarioMode = "both"
    checks: list[Check] = Field(default_factory=list)
    timeout_seconds: float = 120.0  # 由 harness 强制（T3）；schema 先声明
    fragile: bool = False  # 依赖外部动态内容的断言：失败不计入门禁 exit code
    enabled: bool = True  # False = 列出为 [DISABLED]、不跑
    follow_up: list[str] = Field(default_factory=list)  # 多轮场景第 2..n 轮 prompt

    @field_validator("id", "prompt")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @field_validator("tags")
    @classmethod
    def _known_tags(cls, tags: list[str]) -> list[str]:
        unknown = [tag for tag in tags if tag not in KNOWN_TAGS]
        if unknown:
            raise ValueError(f"unknown tag(s) {unknown}; known tags = {list(KNOWN_TAGS)}")
        return tags or ["acceptance"]  # 缺省 = acceptance（P4-D2）

    @model_validator(mode="after")
    def _has_checks(self) -> Scenario:
        if not self.checks:
            raise ValueError(f"scenario {self.id!r} declares no checks（空场景 = 必然通过）")
        return self


class ScenarioLoadError(ValueError):
    """场景文件加载失败：带**文件名**（+ 原始校验错误），便于直接定位到 YAML。"""


def load_scenarios(directory: Path) -> list[Scenario]:
    """加载目录下全部 ``*.yaml`` 场景（按文件名排序），逐条校验。

    任何一条不合法 → :class:`ScenarioLoadError`（含文件名）；id 重复 → 报错并指出两个文件
    （同一场景跑两遍会悄悄污染成功率统计）；目录里一条场景都没有 → 报错（空跑全绿没有意义）。
    """
    scenarios: list[Scenario] = []
    seen: dict[str, Path] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"scenario file must be a mapping, got {type(data).__name__}")
            scenario = Scenario(**data)
        except (yaml.YAMLError, ValidationError, TypeError, ValueError) as exc:
            raise ScenarioLoadError(f"{path.name}: {exc}") from exc
        previous = seen.get(scenario.id)
        if previous is not None:
            raise ScenarioLoadError(
                f"{path.name}: duplicate scenario id {scenario.id!r} "
                f"(already defined in {previous.name})"
            )
        seen[scenario.id] = path
        scenarios.append(scenario)
    if not scenarios:
        raise ScenarioLoadError(f"no scenario files (*.yaml) in {directory}")
    return scenarios
