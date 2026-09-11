"""量化统计 + 双格式报告（P4-D3/D4）。

评测体系的"读数"这一半：把逐场景结果聚合成可引用的数字，再渲染成机器可读（JSON）与
人读（Markdown）两种形态。执行面在 :mod:`eval.harness`，选择/打印/exit code 在
:mod:`eval.run_eval`；本模块**不认识** provider/executor/scenario，只吃
:class:`ScenarioResult` 列表——聚合是纯函数（不碰 IO），渲染才落盘。

**聚合口径**（报告里每个数字都要能被复算，否则文章引用不了）：

- ``success_rate`` = ``passed / executed``，分母是**实际跑过**的样本（跳过/停用的场景没跑，
  不进分母；也不把"环境不匹配"读成失败）。0 个可执行样本时是 ``None`` 而不是 0%/100% ——
  "没跑"与"全绿/全红"是不同的事实。
- ``failed`` 只数**非 fragile** 失败（门禁口径，与 exit code 同一套）；fragile 失败照实记 FAIL、
  单独计数（P4-D6）—— 成功率仍是全样本口径，读者可据 ``passed/failed/fragile_failed`` 复算。
- P50/P95 用**线性插值**（numpy 默认口径）：n=1 时 P50 == P95 == 该样本，空集是 ``None``；
  ``latency_samples`` 一并给出，样本少时别把 P95 当结论。
- token/cost 遵循 fail-loud：采集不到就 ``None``（**不进样本、不填 0**），
  ``total_tokens`` / ``total_cost_usd`` 只对已知样本求和（``token_samples`` / ``cost_samples``
  说明有几个样本）。报表里的 0 只可能是真的 0。
- 延迟/成功率在 :class:`Stats` 里就四舍五入（ms 2 位、率 4 位、成本 6 位）：报告是展示物，
  微秒级尾数不是信息。

**双格式**（P4-D4）：``<timestamp>.json`` 进 ``data/eval-reports/``（data/ 是 gitignore，不入库），
``<timestamp>.md`` 给人读。两者同一时间戳、同一份数据：环境行（日期/模式/模型/git HEAD 短号）+
汇总 + per-tier 拆分 + 场景表（id/tier/tags/PASS|FAIL|SKIP|DISABLED/耗时/tokens/stop_reason/失败明细）。

**多轮**（P4-D7）：``--repeat N`` 的每一轮各自聚合（:class:`Round`），跨轮给 min/max/mean/stdev
（:class:`Variance`）与"哪条场景跨轮结论不一致"（:func:`flaky_scenarios`）——P1 的教训是
"单轮 100% 可靠"不足以支撑"可靠"，需要方差证据。单轮报告没有跨轮段（没有轮可跨）。

**路径规则**（``--json`` / ``--md``）：带后缀 = 文件；结尾分隔符 / 已存在目录 / 无后缀 = 目录，
写入 ``<目录>/<时间戳>.<后缀>``（P4-D7 的 ``--md data/eval-reports/`` 就是这个形状）。
"""

from __future__ import annotations

import json
import math
import statistics
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
DISABLED = "DISABLED"

DASH = "—"
"""报表里"这个字段没有数据"的显示值：与 0 区分开（0 只可能是真的 0）。"""

TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
"""报告文件名的时间戳格式（文件名安全，本地时区）。"""

_SCENARIO_HEADERS = ("id", "tier", "tags", "结果", "耗时 (ms)", "tokens", "stop_reason", "明细")
_TIER_HEADERS = (
    "tier",
    "执行",
    "通过",
    "失败",
    "fragile 失败",
    "跳过",
    "停用",
    "成功率",
    "P50 (ms)",
    "P95 (ms)",
    "tokens",
)
_ROUND_HEADERS = ("轮次",) + _TIER_HEADERS[1:]
_SUMMARY_HEADERS = ("指标", "值")
_VARIANCE_HEADERS = ("指标", "最小", "最大", "均值", "标准差")

_ENV_LABELS = {"date": "日期", "mode": "模式", "model": "模型", "git_commit": "git"}
_ENV_ORDER = ("date", "mode", "model", "git_commit")


# ── 结果行 ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScenarioResult:
    """一条场景在一次运行里的结果 = 报告表的一行。

    ``mode`` 是**本次运行**的模式（mock/real），不是场景声明的 ``mode``（后者解释的是"为什么跳过"，
    写在 ``skip_reason`` 里）。``passed`` 只对"跑过"的场景有意义：跳过/停用的行 ``passed=False``，
    状态由 :attr:`status` 按"停用 → 跳过 → 通过/失败"的次序判定。
    """

    id: str
    tier: str
    tags: tuple[str, ...] = ()
    mode: str = "mock"
    passed: bool = False
    skipped: bool = False
    disabled: bool = False
    fragile: bool = False
    duration_ms: float | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    stop_reason: str | None = None
    failure_detail: str | None = None
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", tuple(self.tags))

    @property
    def status(self) -> str:
        """``PASS`` / ``FAIL`` / ``SKIP`` / ``DISABLED``（停用最根本，其次跳过）。"""
        if self.disabled:
            return DISABLED
        if self.skipped:
            return SKIP
        return PASS if self.passed else FAIL

    @property
    def executed(self) -> bool:
        """真的跑过吗（跳过/停用 = 没跑过）。"""
        return not (self.skipped or self.disabled)

    def as_dict(self) -> dict[str, Any]:
        """JSON 形状（键序稳定：报告 diff 时要看得懂）。"""
        return {
            "id": self.id,
            "tier": self.tier,
            "tags": list(self.tags),
            "mode": self.mode,
            "status": self.status,
            "passed": self.passed,
            "skipped": self.skipped,
            "disabled": self.disabled,
            "fragile": self.fragile,
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "stop_reason": self.stop_reason,
            "failure_detail": self.failure_detail,
            "skip_reason": self.skip_reason,
        }


# ── 聚合（纯函数） ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Stats:
    """一组样本的量化读数（单轮 / 单个 tier / 跨轮样本池都用它）。"""

    total: int = 0
    passed: int = 0
    failed: int = 0  # 非 fragile 失败（门禁口径）
    fragile_failed: int = 0
    skipped: int = 0
    disabled: int = 0
    executed: int = 0
    success_rate: float | None = None  # passed / executed；0 个样本 → None
    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None
    latency_ms_min: float | None = None
    latency_ms_max: float | None = None
    latency_samples: int = 0
    total_tokens: int = 0
    token_samples: int = 0
    total_cost_usd: float = 0.0
    cost_samples: int = 0


@dataclass(frozen=True)
class Aggregates(Stats):
    """整份报告的汇总：``Stats`` 的字段 + per-tier 拆分（键按 tier 名排序，稳定）。"""

    per_tier: dict[str, Stats] = field(default_factory=dict)


def percentile(values: Sequence[float], p: float) -> float | None:
    """线性插值分位数（numpy 默认口径）；空集 → ``None``，单样本 → 该样本。"""
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p must be in [0, 1], got {p!r}")
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _round_ms(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _stats(results: Sequence[ScenarioResult]) -> Stats:
    """一组结果的聚合（口径见模块 docstring）。"""
    executed = [result for result in results if result.executed]
    passed = sum(1 for result in executed if result.passed)
    fragile_failed = sum(1 for result in executed if not result.passed and result.fragile)
    failed = len(executed) - passed - fragile_failed
    durations = [result.duration_ms for result in executed if result.duration_ms is not None]
    tokens = [result.tokens for result in executed if result.tokens is not None]
    costs = [result.cost_usd for result in executed if result.cost_usd is not None]
    return Stats(
        total=len(results),
        passed=passed,
        failed=failed,
        fragile_failed=fragile_failed,
        skipped=sum(1 for result in results if result.skipped),
        disabled=sum(1 for result in results if result.disabled),
        executed=len(executed),
        success_rate=round(passed / len(executed), 4) if executed else None,
        latency_ms_p50=_round_ms(percentile(durations, 0.5)),
        latency_ms_p95=_round_ms(percentile(durations, 0.95)),
        latency_ms_min=_round_ms(min(durations)) if durations else None,
        latency_ms_max=_round_ms(max(durations)) if durations else None,
        latency_samples=len(durations),
        total_tokens=sum(tokens),
        token_samples=len(tokens),
        total_cost_usd=round(sum(costs), 6),
        cost_samples=len(costs),
    )


def aggregate(results: Sequence[ScenarioResult]) -> Aggregates:
    """整份报告的汇总：总体 ``Stats`` + per-tier 拆分（纯函数，不碰 IO）。"""
    results = list(results)
    summary = _stats(results)
    by_tier: dict[str, list[ScenarioResult]] = {}
    for result in results:
        by_tier.setdefault(result.tier, []).append(result)
    return Aggregates(
        **{f.name: getattr(summary, f.name) for f in fields(Stats)},
        per_tier={tier: _stats(by_tier[tier]) for tier in sorted(by_tier)},
    )


@dataclass(frozen=True)
class Round:
    """一轮运行（``--repeat N`` 的第 ``index`` 轮，1 起）。``stats`` 缺省由 ``results`` 现算。"""

    index: int
    results: list[ScenarioResult]
    stats: Stats | None = None

    def __post_init__(self) -> None:
        if self.stats is None:
            object.__setattr__(self, "stats", aggregate(self.results))

    @property
    def summary(self) -> Stats:
        """一定非 ``None`` 的 ``stats`` 视图（``__post_init__`` 已补齐；类型收窄用）。"""
        return self.stats if self.stats is not None else aggregate(self.results)


VARIANCE_METRICS: dict[str, str] = {
    "success_rate": "成功率",
    "latency_ms_p50": "延迟 P50 (ms)",
    "latency_ms_p95": "延迟 P95 (ms)",
    "total_tokens": "总 token",
}
"""跨轮波动的指标（键 = ``Stats`` 字段名，值 = 报表标签）。"""


@dataclass(frozen=True)
class Variance:
    """跨轮波动（总体标准差；1 轮时 stdev=0，此时"波动"没有意义 —— 由报告标注轮次）。"""

    min: float
    max: float
    mean: float
    stdev: float


def variance(rounds: Sequence[Round], metric: str) -> Variance | None:
    """某个指标跨轮的 min/max/mean/stdev；该指标全为 ``None``（没有样本）→ ``None``。"""
    values = [
        float(value) for value in (getattr(r.summary, metric) for r in rounds) if value is not None
    ]
    if not values:
        return None
    return Variance(
        min=min(values),
        max=max(values),
        mean=round(statistics.fmean(values), 6),
        stdev=round(statistics.pstdev(values), 6),
    )


def round_variances(rounds: Sequence[Round]) -> dict[str, Variance]:
    """全部 :data:`VARIANCE_METRICS` 的跨轮波动（没有样本的指标不出现）。"""
    computed = ((metric, variance(rounds, metric)) for metric in VARIANCE_METRICS)
    return {metric: value for metric, value in computed if value is not None}


def flaky_scenarios(rounds: Sequence[Round]) -> list[dict[str, Any]]:
    """跨轮结论不一致的场景（P4-D7 的"单轮 flaky 标注"）。

    只看**跑过**的轮次（跳过/停用不算一种"结论"）；同一场景在各轮出现了 ≥2 种状态才算 flaky。
    """
    statuses: dict[str, list[str]] = {}
    for round_ in rounds:
        for result in round_.results:
            if not result.executed:
                continue
            statuses.setdefault(result.id, []).append(result.status)
    return [
        {"id": scenario_id, "statuses": seen}
        for scenario_id, seen in sorted(statuses.items())
        if len(set(seen)) > 1
    ]


# ── 环境行 ───────────────────────────────────────────────────────────


def git_commit(repo_dir: Path | None = None) -> str | None:
    """``git rev-parse --short HEAD``（best-effort：没装 git / 不在仓库 → ``None``，不抛）。

    报告要能回答"这批数字对应哪份代码"；拿不到就如实写 ``None``（未知不是某个号）。
    """
    cwd = Path(repo_dir) if repo_dir is not None else Path(__file__).resolve().parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def environment(
    *,
    mode: str,
    model: str | None = None,
    extra: Mapping[str, Any] | None = None,
    when: datetime | None = None,
    repo_dir: Path | None = None,
) -> dict[str, Any]:
    """报告的环境行：日期 / 模式 / 模型 id / git HEAD 短号（+ 调用方补充项）。

    ``extra`` 里值为 ``None`` 的键不写入（"没这个信息"不该在报告里渲染成一个空）。
    """
    env: dict[str, Any] = {
        "date": (when or datetime.now().astimezone()).isoformat(timespec="seconds"),
        "mode": mode,
        "model": model,
        "git_commit": git_commit(repo_dir),
    }
    for key, value in (extra or {}).items():
        if value is not None:
            env[key] = value
    return env


def timestamp(when: datetime | None = None) -> str:
    """报告文件名的时间戳（本地时区；一对 JSON/MD 共用同一个）。"""
    return (when or datetime.now().astimezone()).strftime(TIMESTAMP_FORMAT)


def resolve_output_path(path: str | Path, *, suffix: str, stamp: str | None = None) -> Path:
    """把 ``--json`` / ``--md`` 的取值解析成**具体文件路径**（父目录自动创建）。

    规则：结尾分隔符 / 已存在目录 / **无后缀** → 目录，写入 ``<目录>/<时间戳><suffix>``
    （P4-D4 的 ``<timestamp>.json`` 形状）；带后缀 → 就是文件路径。
    """
    raw = str(path)
    target = Path(path)
    if raw.endswith(("/", "\\")) or target.is_dir() or target.suffix == "":
        target = target / f"{stamp or timestamp()}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


# ── 渲染：JSON ───────────────────────────────────────────────────────


_STATS_KEYS = tuple(f.name for f in fields(Stats))


def _stats_dict(stats: Stats) -> dict[str, Any]:
    return {name: getattr(stats, name) for name in _STATS_KEYS}


def _rounds(
    rounds: Sequence[Round] | None, results: Sequence[ScenarioResult], aggregates: Aggregates
) -> list[Round]:
    """逐轮结构；调用方没给 ``rounds`` 时把 ``results`` 当作单轮。"""
    if rounds:
        return list(rounds)
    return [Round(index=1, results=list(results), stats=aggregates)]


def write_json(
    aggregates: Aggregates,
    results: Sequence[ScenarioResult],
    path: str | Path,
    *,
    environment: Mapping[str, Any] | None = None,
    rounds: Sequence[Round] | None = None,
    stamp: str | None = None,
) -> Path:
    """写机器可读报告；返回**实际写入**的路径（目录形态下带时间戳，调用方拿它打日志）。

    ``aggregates`` 必须是 ``results`` 的聚合（同一批样本）；``rounds`` 给逐轮明细，
    缺省 = 单轮（``results`` 即那一轮）。跨轮方差/flaky 只在 >1 轮时出现 —— 单轮没有"跨轮"。
    """
    round_list = _rounds(rounds, results, aggregates)
    payload: dict[str, Any] = {
        "environment": dict(environment or {}),
        "summary": _stats_dict(aggregates),
        "per_tier": {tier: _stats_dict(stats) for tier, stats in aggregates.per_tier.items()},
        "rounds": [
            {
                "index": round_.index,
                "stats": _stats_dict(round_.summary),
                "scenarios": [result.as_dict() for result in round_.results],
            }
            for round_ in round_list
        ],
    }
    if len(round_list) > 1:
        payload["variance"] = {
            metric: asdict(value) for metric, value in round_variances(round_list).items()
        }
        payload["flaky"] = flaky_scenarios(round_list)
    target = resolve_output_path(path, suffix=".json", stamp=stamp)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


# ── 渲染：Markdown ───────────────────────────────────────────────────


def _cell(value: Any) -> str:
    """表格单元格：``None``/空 → ``—``；竖线转义、换行压平（明细不能撑破表格）。"""
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\n", " ").strip()
    return text or DASH


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(_cell(header) for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines += ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]
    return lines


def _fmt_ms(value: float | None) -> str:
    return DASH if value is None else f"{value:.1f}"


def _fmt_rate(stats: Stats) -> str:
    if stats.success_rate is None or not stats.executed:
        return f"{DASH}（无可执行样本）"
    return f"{stats.success_rate * 100:.1f}% ({stats.passed}/{stats.executed})"


def _fmt_tokens(stats: Stats) -> str:
    if not stats.token_samples:
        return f"{DASH}（未提供）"
    return f"{stats.total_tokens}（{stats.token_samples} 条样本）"


def _fmt_cost(stats: Stats) -> str:
    if not stats.cost_samples:
        return f"{DASH}（未提供）"
    return f"${stats.total_cost_usd:.6f}（{stats.cost_samples} 条样本）"


def _env_lines(env: Mapping[str, Any]) -> list[str]:
    keys = [key for key in _ENV_ORDER if key in env]
    keys += sorted(key for key in env if key not in _ENV_ORDER)
    return [f"- {_ENV_LABELS.get(key, key)}: {_cell(env[key])}" for key in keys]


def _summary_table(stats: Stats) -> list[str]:
    rows = [
        ("场景数", stats.total),
        ("通过", stats.passed),
        ("失败（门禁）", stats.failed),
        ("失败（fragile，不计门禁）", stats.fragile_failed),
        ("跳过", stats.skipped),
        ("停用", stats.disabled),
        ("实际执行", stats.executed),
        ("成功率", _fmt_rate(stats)),
        ("延迟 P50 (ms)", _fmt_ms(stats.latency_ms_p50)),
        ("延迟 P95 (ms)", _fmt_ms(stats.latency_ms_p95)),
        (
            "延迟 min / max (ms)",
            f"{_fmt_ms(stats.latency_ms_min)} / {_fmt_ms(stats.latency_ms_max)}",
        ),
        ("总 token", _fmt_tokens(stats)),
        ("总成本 (USD)", _fmt_cost(stats)),
    ]
    return _md_table(_SUMMARY_HEADERS, rows)


def _stats_cells(stats: Stats) -> list[str]:
    return [
        str(stats.executed),
        str(stats.passed),
        str(stats.failed),
        str(stats.fragile_failed),
        str(stats.skipped),
        str(stats.disabled),
        _fmt_rate(stats),
        _fmt_ms(stats.latency_ms_p50),
        _fmt_ms(stats.latency_ms_p95),
        str(stats.total_tokens) if stats.token_samples else DASH,
    ]


def _tier_table(per_tier: Mapping[str, Stats]) -> list[str]:
    rows = [[tier, *_stats_cells(stats)] for tier, stats in per_tier.items()]
    return _md_table(_TIER_HEADERS, rows)


def _round_table(rounds: Sequence[Round]) -> list[str]:
    rows = [[str(round_.index), *_stats_cells(round_.summary)] for round_ in rounds]
    return _md_table(_ROUND_HEADERS, rows)


_VARIANCE_FORMATS = {
    "success_rate": "{:.4f}",
    "latency_ms_p50": "{:.2f}",
    "latency_ms_p95": "{:.2f}",
    "total_tokens": "{:.0f}",
}


def _variance_table(rounds: Sequence[Round]) -> list[str]:
    rows = []
    for metric, value in round_variances(rounds).items():
        fmt = _VARIANCE_FORMATS.get(metric, "{:.4f}")
        rows.append(
            [
                VARIANCE_METRICS.get(metric, metric),
                fmt.format(value.min),
                fmt.format(value.max),
                fmt.format(value.mean),
                fmt.format(value.stdev),
            ]
        )
    return _md_table(_VARIANCE_HEADERS, rows)


def _scenario_table(results: Sequence[ScenarioResult]) -> list[str]:
    rows = []
    for result in results:
        if result.status == FAIL:
            detail = result.failure_detail
        elif result.status == SKIP:
            detail = result.skip_reason
        else:
            detail = None
        rows.append(
            [
                result.id,
                result.tier,
                ", ".join(result.tags) if result.tags else DASH,
                result.status,
                _fmt_ms(result.duration_ms),
                DASH if result.tokens is None else str(result.tokens),
                result.stop_reason,
                detail,
            ]
        )
    return _md_table(_SCENARIO_HEADERS, rows)


def write_markdown(
    aggregates: Aggregates,
    results: Sequence[ScenarioResult],
    path: str | Path,
    *,
    environment: Mapping[str, Any] | None = None,
    rounds: Sequence[Round] | None = None,
    stamp: str | None = None,
) -> Path:
    """写人读报告（环境行 + 汇总 + 分层 + 跨轮/波动/flaky + 场景表）；返回实际写入的路径。"""
    round_list = _rounds(rounds, results, aggregates)
    env = dict(environment or {})
    lines: list[str] = ["# BrowsePilot 评测报告", "", *_env_lines(env)]
    lines += ["", "## 汇总", ""]
    if len(round_list) > 1:
        # 多轮时汇总是**样本池**（每轮各计一遍）：不写这句话，"场景数 32" 会被读成跑了 32 条场景。
        lines += [f"（{len(round_list)} 轮样本池：每轮各计一遍；逐轮数字见「跨轮汇总」）", ""]
    lines += _summary_table(aggregates)
    lines += ["", "## 分层汇总", ""]
    lines += _tier_table(aggregates.per_tier) if aggregates.per_tier else ["（无可分层样本）"]
    if len(round_list) > 1:
        lines += ["", f"## 跨轮汇总（{len(round_list)} 轮）", "", *_round_table(round_list)]
        lines += ["", "## 跨轮波动", "", *_variance_table(round_list)]
        flaky = flaky_scenarios(round_list)
        lines += ["", f"## flaky 场景（{len(flaky)} 个跨轮结论不一致）", ""]
        lines += (
            [f"- `{item['id']}`: {', '.join(item['statuses'])}" for item in flaky]
            if flaky
            else ["（无）"]
        )
    lines += ["", "## 场景结果", ""]
    if len(round_list) == 1:
        lines += _scenario_table(round_list[0].results)
    else:
        for round_ in round_list:
            lines += [f"### 第 {round_.index} 轮 / {len(round_list)}", ""]
            lines += [*_scenario_table(round_.results), ""]
    target = resolve_output_path(path, suffix=".md", stamp=stamp)
    target.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return target
