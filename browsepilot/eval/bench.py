"""V2.0 性能基线测量（P5 Task 3）——冷启动 · ``/health`` · tier1 端到端（mock + real）。

这个脚本只做一件事：把**可复现的数字**量出来（P50/P95 + 全部原始样本），不做 PASS/FAIL
判定（断言在 :mod:`eval.checks` / 场景集里）。数字落在 ``docs/v2-performance.md``。

用法（在 ``browsepilot/`` 目录下；必须经 ``uv run`` 跑，量到的才是 venv 解释器）::

    set -a && source .env && set +a          # 真实模式凭据（T7 §7.1 口径；只在进程内）
    export BP_DEFAULT_MODEL=deepseek/deepseek-chat BP_BROWSER_CHANNEL=msedge
    uv run python -m eval.bench --repeat 5 --real-repeat 3 --json data/bench/p5-t3.json

分节（打印与 JSON 一一对应）：

1. ``cold start``：``sys.executable -c`` 子进程（每次**新解释器**）——解释器基线 /
   浅层 import / 完整 ``import browsepilot.app.main``。最后一条 = 真正冷启动：
   依赖链 import + 模块级 ``app = _default_app()``（生产组合根）都在里面。
2. ``create_app``：进程内装配成本（import 已热）——量的是 Settings 归一化 + 路由/middleware
   注册（fake provider/executor，不碰 .env、不建 sqlite、不起浏览器池）。
3. ``/health``：TestClient 的 ASGI 进程内口径（**不含** socket/HTTP 栈）；首次请求单记，
   其余取 P50/P95。
4. ``e2e tier1 (mock)``：``eval.harness.run_scenario``（内部就是
   :func:`browsepilot.agent.api.run_task` + 评测侧记账装饰器）跑 ``t1_weather_beijing``，
   FakeProvider/FakeExecutor → 离线、零 token，读作**噪声基线**。
5. ``e2e tier1 (real)``：同一条场景、真模型 + 真服务（天气走网络）；浏览器池**起一次**并单独
   计时（跨场景共享 → 摊薄）；每个场景跑完 ``executor.aclose()`` 归还页面（与
   ``eval/run_eval.py`` 的逐场景隔离同一条）。无凭据 → 只记 SKIP，mock 段照跑。

**tier2/tier3 的真实延迟不重跑**：引用 ``docs/v2-eval.md`` §7 的 T7 数据（p50/p95 与极值），
本脚本只负责"新的一轮 tier1 独立读数"（见 ``docs/v2-performance.md`` §5）。

凭据（进程内，不落盘）：``BP_MODEL_API_KEY`` 直接可用；否则把 ``OPENAI_API_KEY`` /
``OPENAI_BASE_URL`` **映射**成 ``BP_MODEL_API_KEY`` / ``BP_MODEL_BASE_URL``（T7 §7.1 的做法
固化进脚本）。映射不是便利而是必需：实测 litellm 对 ``deepseek/`` 前缀**不会**回退读
``OPENAI_API_KEY``（→ AuthenticationError），而 ``Settings`` 只读 ``BP_`` 前缀。

与门禁的关系：**不进** ``eval/gate.sh``。真实段要凭据与网络；mock 段虽离线可跑，但耗时读数
天然与机器/负载相关，不适合当阈值（gate 只看断言，不看数字）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from browsepilot.agent.tracer import JsonlTracer
from browsepilot.app.config import Settings
from browsepilot.app.main import create_app
from eval import harness
from eval.fake_deps import FakeExecutor, FakeProvider
from eval.report import percentile
from eval.scenarios import Scenario, load_scenarios

ROOT = Path(__file__).resolve().parent.parent
"""``browsepilot/`` 项目根：``eval/scenarios`` / ``data/`` / ``.env`` 都相对它。"""

SCENARIOS_DIR = ROOT / "eval" / "scenarios"

SCENARIO_ID = "t1_weather_beijing"
"""端到端测量的那条 tier1 任务（与 T7 acceptance 同一条场景，便于对照）。"""

DEFAULT_REPEAT = 5
"""默认重复次数：``/health`` 与 mock 端到端要 >= 5 才有 P95 可读。"""

COLD_START_PROBES: tuple[tuple[str, str], ...] = (
    ("interpreter baseline (python -c pass)", "pass"),
    ("import browsepilot.app.config", "import browsepilot.app.config"),
    ("import browsepilot.app.main (cold start)", "import browsepilot.app.main"),
)
"""三条子进程探测：解释器基线 → 浅层 import → 完整冷启动（含 ``app = _default_app()``）。"""


def summarize(samples: Sequence[float]) -> dict[str, Any]:
    """``n`` / ``P50`` / ``P95`` / ``min`` / ``max``（分位数与 :mod:`eval.report` 同口径：线性插值）。"""
    if not samples:
        raise ValueError("summarize() 至少需要一个样本")
    return {
        "n": len(samples),
        "p50_ms": round(float(percentile(samples, 0.5)), 2),
        "p95_ms": round(float(percentile(samples, 0.95)), 2),
        "min_ms": round(min(samples), 2),
        "max_ms": round(max(samples), 2),
    }


@dataclass
class Section:
    """一个测量分节：状态 + 指标（名 → 样本列表）+ 备注 / 额外事实。"""

    name: str
    status: str = "ok"  # ok | skipped | failed
    note: str = ""
    series: dict[str, list[float]] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def record(self, label: str, value_ms: float) -> None:
        self.series.setdefault(label, []).append(float(value_ms))

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "note": self.note,
            "metrics": {label: summarize(values) for label, values in self.series.items()},
            "samples_ms": {
                label: [round(value, 2) for value in values] for label, values in self.series.items()
            },
            "extra": self.extra,
        }


def print_section(section: Section) -> None:
    """一节的表格（指标行 + 额外事实行）；宽字段留白对不齐也比截断好读。"""
    print(f"\n[{section.name}]  status={section.status}")
    if section.note:
        print(f"    note: {section.note}")
    for label, values in section.series.items():
        stats = summarize(values)
        print(
            f"    {label:<40} n={stats['n']:<3}"
            f" P50={stats['p50_ms']:>10.2f} ms"
            f" P95={stats['p95_ms']:>10.2f} ms"
            f" min={stats['min_ms']:>9.2f} max={stats['max_ms']:>10.2f}"
        )
    for key, value in section.extra.items():
        print(f"    {key}: {value}")


# ── 1. 冷启动（子进程） ──────────────────────────────────────────────


def timed_subprocess(argv: list[str], *, cwd: Path) -> float:
    """跑一次子进程并返回墙钟毫秒（失败直接抛：冷启动量的就是"起不来就没有读数"）。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    start = time.perf_counter()
    subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True)
    return (time.perf_counter() - start) * 1000


def measure_cold_start(repeat: int) -> Section:
    """冷启动：每条探测都开**新解释器**（``sys.executable -c <code>``，cwd=browsepilot/）。

    进程内 import 计时只量得到"缓存后"的成本 —— 冷启动必须换进程，这是本节的立身之本。
    """
    section = Section("1. cold start (fresh interpreter per probe)")
    for label, code in COLD_START_PROBES:
        for _ in range(repeat):
            section.record(label, timed_subprocess([sys.executable, "-c", code], cwd=ROOT))
    baseline = summarize(section.series[COLD_START_PROBES[0][0]])
    cold = summarize(section.series[COLD_START_PROBES[2][0]])
    section.extra["import_cost_p50_ms (cold start - interpreter baseline)"] = round(
        cold["p50_ms"] - baseline["p50_ms"], 2
    )
    return section


# ── 2. create_app 装配（进程内） ──────────────────────────────────────


def measure_create_app(repeat: int, trace_dir: Path) -> Section:
    """``create_app`` 的装配成本（进程内、import 已热）。

    fake provider/executor + ``Settings(_env_file=None, checkpoint_db_path="")``：不读 .env、
    不建 sqlite、不起浏览器池 —— 量的是"把 app 拼起来"本身。
    首次装配单记（实测含惰性初始化：pydantic/FastAPI 的响应模型首次编译 —— 一次 ~13ms vs
    稳态 ~3ms），不计入 P50：P50 要读的是稳态装配成本。
    """
    settings = Settings(_env_file=None, checkpoint_db_path="")
    tracer = JsonlTracer(trace_id="bench-create-app", out_dir=trace_dir)

    def assemble() -> FastAPI:
        return create_app(
            settings,
            provider=FakeProvider(),
            executor=FakeExecutor(),
            tracer=tracer,
        )

    section = Section("2. create_app assembly (in-process, imports warm)")
    start = time.perf_counter()
    assemble()
    section.extra["first_call_ms"] = round((time.perf_counter() - start) * 1000, 2)
    for _ in range(repeat):
        start = time.perf_counter()
        assemble()
        section.record("create_app", (time.perf_counter() - start) * 1000)
    return section


# ── 3. /health（TestClient） ─────────────────────────────────────────


def measure_health(repeat: int, trace_dir: Path) -> Section:
    """``/health``：TestClient 的 ASGI 进程内口径（不含 socket/HTTP 栈，见文档 §3）。

    首次请求单记（含 ASGI/客户端惰性初始化），其余 N 次取 P50/P95；非 200 → ``status=failed``。
    """
    app = create_app(
        Settings(_env_file=None, checkpoint_db_path=""),
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=JsonlTracer(trace_id="bench-health", out_dir=trace_dir),
    )
    section = Section("3. /health (TestClient, in-process ASGI)")
    with TestClient(app) as client:
        start = time.perf_counter()
        first = client.get("/health")
        section.extra["first_request_ms"] = round((time.perf_counter() - start) * 1000, 2)
        if first.status_code != 200:
            section.status = "failed"
            section.note = f"/health 返回 {first.status_code}（预期 200）"
            return section
        section.extra["body"] = first.json()
        for _ in range(repeat):
            start = time.perf_counter()
            response = client.get("/health")
            elapsed = (time.perf_counter() - start) * 1000
            if response.status_code != 200:
                section.status = "failed"
                section.note = f"/health 返回 {response.status_code}（预期 200）"
                break
            section.record("GET /health", elapsed)
    return section


# ── 4. 端到端：tier1（mock） ─────────────────────────────────────────


def load_scenario(scenario_id: str = SCENARIO_ID) -> Scenario:
    """按 id 取场景（未知 id 是**加载错误**，fail-loud，与场景集加载同一原则）。"""
    for scenario in load_scenarios(SCENARIOS_DIR):
        if scenario.id == scenario_id:
            return scenario
    raise SystemExit(f"scenario {scenario_id!r} not found in {SCENARIOS_DIR}")


def measure_mock_e2e(repeat: int, scenario: Scenario, trace_dir: Path) -> Section:
    """tier1 端到端（mock）：FakeProvider/FakeExecutor，离线、零 token → **噪声基线**。

    跑 ``eval.harness.run_scenario``（内部即 ``run_task``；装饰器只加评测侧记账），
    取任务墙钟 ``duration_ms``（口径与 T7 报告逐字一致）。
    """
    section = Section(f"4. tier1 e2e mock ({scenario.id})")

    async def run_all() -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for index in range(repeat):
            outcomes.append(
                await harness.run_scenario(
                    scenario,
                    provider=FakeProvider(),
                    executor=FakeExecutor(),
                    tracer=JsonlTracer(trace_id=f"bench-mock-{index}", out_dir=trace_dir),
                )
            )
        return outcomes

    outcomes = asyncio.run(run_all())
    for outcome in outcomes:
        section.record("duration_ms", float(outcome["duration_ms"]))
    reasons = sorted({str(outcome.get("stop_reason")) for outcome in outcomes})
    section.extra["stop_reason"] = reasons
    section.extra["tools_called"] = sorted(
        {", ".join(outcome.get("tool_calls") or []) for outcome in outcomes}
    )
    section.extra["token_usage"] = (
        "无 token 数字（FakeProvider 不报 usage；快照只有 chat_calls/model_calls 覆盖度键）"
    )
    if reasons != ["completed"]:
        section.status = "failed"
        section.note = f"mock 跑出现非 completed 的 stop_reason：{reasons}"
    return section


# ── 5. 端到端：tier1（real） ─────────────────────────────────────────


def real_credentials() -> tuple[bool, str]:
    """real 段凭据前置 → ``(可用, 说明)``；不可用**不抛**，由调用方记 SKIP 并继续 mock 段。

    ``BP_MODEL_API_KEY`` 为空时把 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` 映射成
    ``BP_MODEL_API_KEY`` / ``BP_MODEL_BASE_URL``（T7 §7.1 的做法固化进脚本；只改本进程环境，
    不落盘）。实测 litellm 对 ``deepseek/`` 前缀不会回退读 ``OPENAI_API_KEY`` → 没有映射时
    是 AuthenticationError，而 ``Settings`` 只读 ``BP_`` 前缀，两侧都不会自己接上。
    """
    if os.environ.get("BP_MODEL_API_KEY"):
        return True, "凭据：BP_MODEL_API_KEY（来自环境）"
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        os.environ["BP_MODEL_API_KEY"] = openai_key
        if os.environ.get("OPENAI_BASE_URL"):
            os.environ.setdefault("BP_MODEL_BASE_URL", os.environ["OPENAI_BASE_URL"])
        return True, "凭据：BP_MODEL_API_KEY ← OPENAI_API_KEY（进程内映射，T7 §7.1 口径）"
    if Settings().model_api_key:
        return True, "凭据：BP_MODEL_API_KEY（来自 .env）"
    return False, "无 BP_MODEL_API_KEY / OPENAI_API_KEY → real 段 SKIP（mock 段照跑）"


def measure_real_e2e(
    repeat: int, scenario: Scenario, trace_dir: Path, *, note: str = ""
) -> Section:
    """tier1 端到端（real）：真模型 + 真服务 + 浏览器池（起一次，单独计时）。

    - 组合根 / 池生命周期 / 逐场景隔离与评测完全同源：``harness.build_real_deps`` +
      ``harness.pool_lifecycle`` + 每场景后 ``executor.aclose()``（C2③）。
    - 单次尝试异常**不吞**：记进 ``errors`` 并继续（bench 要把 mock 结果与失败原因一起交出来，
      而不是中途退出）；全部尝试都异常 → ``status=failed``。
    - 池起不来（channel 写错 / 没装浏览器）不致命：tier1 不用浏览器，如实记 ``pool_started``。
    """
    section = Section(f"5. tier1 e2e real ({scenario.id})")
    section.note = note

    async def run_all() -> dict[str, Any]:
        deps = harness.build_real_deps()
        rows: list[dict[str, Any]] = []
        pool_start_ms: float | None = None
        pool_started = False
        async with contextlib.AsyncExitStack() as stack:
            start = time.perf_counter()
            pool_started = await stack.enter_async_context(
                harness.pool_lifecycle(deps.pool, deps.executor)
            )
            pool_start_ms = (time.perf_counter() - start) * 1000
            for index in range(repeat):
                try:
                    outcome = await harness.run_scenario(
                        scenario,
                        provider=deps.provider,
                        executor=deps.executor,
                        tracer=JsonlTracer(trace_id=f"bench-real-{index}", out_dir=trace_dir),
                    )
                    rows.append(
                        {
                            "ok": True,
                            "duration_ms": float(outcome["duration_ms"]),
                            "stop_reason": str(outcome.get("stop_reason")),
                            "token_usage": outcome.get("token_usage"),
                            "tool_calls": list(outcome.get("tool_calls") or []),
                            "answer_has_city": "北京" in str(outcome.get("final_answer") or ""),
                        }
                    )
                except Exception as exc:  # 记下来继续跑（见 docstring）
                    rows.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                finally:
                    with contextlib.suppress(Exception):
                        await deps.executor.aclose()
        return {"rows": rows, "pool_start_ms": pool_start_ms, "pool_started": pool_started}

    result = asyncio.run(run_all())
    rows: list[dict[str, Any]] = result["rows"]
    ok_rows = [row for row in rows if row["ok"]]
    for row in ok_rows:
        section.record("duration_ms", row["duration_ms"])
    snapshots = [row["token_usage"] for row in ok_rows if row.get("token_usage")]
    section.extra["pool_start_ms"] = (
        None if result["pool_start_ms"] is None else round(result["pool_start_ms"], 2)
    )
    section.extra["pool_started"] = result["pool_started"]
    section.extra["stop_reason"] = sorted({str(row.get("stop_reason")) for row in ok_rows})
    section.extra["tools_called"] = sorted({", ".join(row.get("tool_calls") or []) for row in ok_rows})
    section.extra["answer_has_city (北京)"] = (
        f"{sum(1 for row in ok_rows if row['answer_has_city'])}/{len(ok_rows)}"
    )
    section.extra["tokens_per_run"] = [
        (row.get("token_usage") or {}).get("total_tokens") for row in ok_rows
    ]
    total_tokens = sum(int(item.get("total_tokens") or 0) for item in snapshots)
    measured = sum(
        int(item.get("chat_calls", 0)) - int(item.get("estimated_calls") or 0) for item in snapshots
    )
    model_calls = sum(int(item.get("model_calls", 0)) for item in snapshots)
    section.extra["tokens_total"] = total_tokens
    section.extra["token_coverage"] = (
        f"{measured}/{model_calls} 模型调用有计量（chat-only 口径 → 下界）"
    )
    errors = [row["error"] for row in rows if not row["ok"]]
    section.extra["errors"] = errors
    if not ok_rows:
        section.status = "failed"
        section.note = (f"{section.note} | " if section.note else "") + "全部尝试异常（见 errors）"
    return section


# ── 环境与入口 ───────────────────────────────────────────────────────


def git(*args: str) -> str:
    """``git <args>`` 的输出（失败/无 git → ``"unknown"``：环境行不该让 bench 崩）。"""
    try:
        done = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return done.stdout.strip()


def uv_version() -> str:
    try:
        done = subprocess.run(["uv", "--version"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return done.stdout.strip()


def environment(repeat: int, real_repeat: int, *, real_note: str) -> dict[str, Any]:
    """运行环境（写进 JSON；文档 §1 逐字取自这里：日期/模型/机器/Python/浏览器口径）。"""
    settings = Settings()
    return {
        "date": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_commit": git("rev-parse", "--short", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "python": f"{platform.python_version()} @ {sys.executable}",
        "platform": platform.platform(),
        "machine": f"{platform.machine()} / {platform.processor() or 'n/a'} / {os.cpu_count()} cpus",
        "model": settings.default_model,
        "browser": (
            f"channel={settings.browser_channel or 'playwright-default'} "
            f"headless={settings.browser_headless} max_pages={settings.browser_max_pages}"
        ),
        "uv": uv_version(),
        "repeat": repeat,
        "real_repeat": real_repeat,
        "credentials": real_note,
    }


def force_utf8_stdout() -> None:
    """Windows 管道里按 UTF-8 输出（否则中文表头会被按 locale 编码成乱码字节）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.bench",
        description=(
            "BrowsePilot V2.0 性能基线测量（P5-T3）：冷启动 / create_app / /health / "
            "tier1 端到端（mock + real）。只量数字，不做 PASS/FAIL。"
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=DEFAULT_REPEAT,
        help=f"每项重复次数（默认 {DEFAULT_REPEAT}）；real 段默认同值，用 --real-repeat 覆盖",
    )
    parser.add_argument(
        "--real-repeat", type=int, default=None, help="real 端到端重复次数（默认 = --repeat）"
    )
    parser.add_argument(
        "--skip-real",
        action="store_true",
        help="跳过 real 段（无凭据 / 不花 token / 离线）",
    )
    parser.add_argument(
        "--json", type=Path, default=None, help="把环境 + 全部原始样本写成 JSON（证据留存）"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeat < 1 or (args.real_repeat is not None and args.real_repeat < 1):
        raise SystemExit("--repeat / --real-repeat 必须 >= 1")
    force_utf8_stdout()
    real_repeat = args.real_repeat or args.repeat
    available, credential_note = real_credentials()
    env = environment(args.repeat, real_repeat, real_note=credential_note)

    print("BrowsePilot V2 performance baseline (P5-T3)")
    for key, value in env.items():
        print(f"  {key}: {value}")

    scenario = load_scenario()
    real_section_name = f"5. tier1 e2e real ({scenario.id})"
    with tempfile.TemporaryDirectory(prefix="browsepilot-bench-") as tmp:
        trace_dir = Path(tmp)
        sections = [
            measure_cold_start(args.repeat),
            measure_create_app(args.repeat, trace_dir),
            measure_health(args.repeat, trace_dir),
            measure_mock_e2e(args.repeat, scenario, trace_dir),
        ]
        if args.skip_real:
            sections.append(Section(real_section_name, status="skipped", note="--skip-real"))
        elif not available:
            sections.append(Section(real_section_name, status="skipped", note=credential_note))
        else:
            try:
                sections.append(
                    measure_real_e2e(real_repeat, scenario, trace_dir, note=credential_note)
                )
            except Exception as exc:
                # real 段整体炸了（如 build_real_deps / Settings 报错）不要吞掉已经量到的 mock 读数：
                # 记一条 failed 分节 + 原因，JSON/表格照写（“把 mock 结果与失败原因一起交出来”）。
                sections.append(
                    Section(
                        real_section_name,
                        status="failed",
                        note=f"real 段未跑完：{type(exc).__name__}: {exc}",
                    )
                )

    for section in sections:
        print_section(section)

    if args.json:
        payload = {"environment": env, "sections": [section.to_json() for section in sections]}
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nJSON written: {args.json}")

    failed = [section.name for section in sections if section.status == "failed"]
    if failed:
        print(f"\n[bench] {len(failed)} 个分节失败（读数不合格）：{failed}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
