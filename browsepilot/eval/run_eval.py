"""Eval runner (v0.3) — executes scenarios against the Tier1/2/3 pipeline.

Modes:
  --mock   : fake provider/executor (no network, no tokens) — CI mode
  default  : real model + services + browser pool (needs BP_MODEL_API_KEY) — acceptance mode

真实模式（P4 评测体系的前置接线，运维前置见 docs/v2-p3-report.md §十二「P4 评测前置（运维）」）：

- **注入浏览器池**：``AppToolExecutor`` 拿不到池时 6 个 Tier3 工具统一返回
  ``browser_unavailable`` —— tier3 场景会**全部**失败，而那是接线问题不是任务失败
  （P3 final review 给 P4 的关键输入）。所以 real 模式走同一个组合根：Settings →
  BrowserPool + AppToolExecutor（策略也从同一个 Settings 显式构造，I3）。
  池起不来（本机没装 chromium rev / channel 写错）时打 WARNING 继续评测 Tier1/2：
  宁可让 tier3 的失败在报告里**可见地**归因到环境，也不要静默产出误导性的成功率。
- **逐场景隔离**（C2③）：每个场景跑完 ``executor.aclose()`` 把当前页还给池。不隔离的话
  下一个场景的 ``browser_snapshot`` 可能拍到上一个场景留下的页面，报 completed 却答非所问
  （C2b：未与任务目标匹配的快照不得算完成）。
- 池的生命周期跨场景（``_run_real`` 的 start/close），**页面**的生命周期逐场景；
  因此 ``run_all`` 本身是 async（一个事件循环跑完全部场景）。

``finally`` 里跑 teardown：场景自己炸了（模型/服务异常）也要归还页面，否则脏页留给下一个场景。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from browsepilot.agent.api import run_task
from browsepilot.agent.tracer import JsonlTracer, Tracer
from eval.fake_deps import FakeExecutor, FakeProvider
from eval.scenarios import Check, Scenario, load_scenarios

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


def check_outcome(outcome: dict, check: Check) -> tuple[bool, str]:
    """Evaluate one check against a run_task outcome dict. Returns (passed, detail)."""
    if check.type == "field":
        value = outcome.get("result_data", {}).get(check.field)
        if check.kind == "number":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            return ok, f"{check.field}={value!r} (number)"
        ok = value == check.expected
        return ok, f"{check.field}={value!r} expected={check.expected!r}"
    if check.type == "contains_keyword":
        text = outcome.get("final_answer", "")
        hits = [k for k in (check.keywords or []) if k in text]
        return bool(hits), f"keywords hit={hits}"
    return False, f"unknown check type {check.type}"


async def _run_scenario(
    scenario: Scenario, *, provider, executor, tracer, teardown=None
) -> tuple[dict, list[tuple[bool, str]]]:
    """跑一个场景并评估 checks；``teardown``（real 模式 = ``executor.aclose``）在 finally 里跑。"""
    try:
        outcome = await run_task(
            scenario.prompt, provider=provider, executor=executor, tracer=tracer
        )
        checks = [check_outcome(outcome, c) for c in scenario.checks]
    finally:
        if teardown is not None:
            await teardown()
    return outcome, checks


async def run_all(*, provider, executor, tracer: Tracer, teardown=None) -> int:
    """顺序跑全部场景并打印逐场景结果；``teardown`` 在每个场景之后跑（逐场景隔离）。"""
    scenarios = load_scenarios(SCENARIOS_DIR)
    print(f"[browsepilot eval v0.3] {len(scenarios)} scenarios loaded")
    failed = 0
    for s in scenarios:
        outcome, checks = await _run_scenario(
            s, provider=provider, executor=executor, tracer=tracer, teardown=teardown
        )
        ok = all(passed for passed, _ in checks)
        failed += 0 if ok else 1
        detail = "; ".join(d for _, d in checks)
        print(f"  [{'PASS' if ok else 'FAIL'}] {s.id}: {detail}")
    print(f"[eval] {len(scenarios) - failed}/{len(scenarios)} passed")
    return 1 if failed else 0


def _real_stack():
    """真实模式的组合根：Settings → 模型 + Tier1/2 服务 + 浏览器池 + executor + 策略。

    导入刻意留在函数里：``--mock``（CI）不该拉起 litellm / playwright / ddgs 的依赖链。
    池配置与 app（``app.main._default_app``）同源：headless / max_pages / channel（空串 → None）。
    """
    from browsepilot.app.browser.browser_pool import BrowserPool
    from browsepilot.app.browser.policy import NavigationPolicy
    from browsepilot.app.config import Settings
    from browsepilot.app.executor import AppToolExecutor
    from browsepilot.app.services.fetch import PageFetchService
    from browsepilot.app.services.search import SearchService
    from browsepilot.app.services.weather import WeatherService
    from browsepilot.model.litellm_provider import LiteLLMProvider

    settings = Settings()
    provider = LiteLLMProvider(
        settings.default_model,
        api_key=settings.model_api_key or None,
        base_url=settings.model_base_url or None,
    )
    pool = BrowserPool(
        headless=settings.browser_headless,
        max_pages=settings.browser_max_pages,
        channel=settings.browser_channel or None,
    )
    executor = AppToolExecutor(
        WeatherService(),
        SearchService(),
        PageFetchService(),
        browser=pool,
        # I3：池与策略都从这**同一个** Settings 构造（不显式传的话 executor 自己再读一次
        # 环境，把显式配置静默覆盖掉）。
        policy=NavigationPolicy(settings.navigation_policy, settings.allowed_domains),
    )
    return provider, executor, pool


async def _run_real(tracer: Tracer) -> int:
    """真实模式：起池 → 逐场景隔离评测 → 关池；池起不来就诚实降级（打警告，不静默）。"""
    provider, executor, pool = _real_stack()
    started = False
    try:
        await pool.start()
        started = True
    except Exception as exc:
        print(
            f"[eval] WARNING: BrowserPool.start() failed ({exc}); "
            "Tier3 工具一律 browser_unavailable —— 这些 tier3 失败是环境问题，不是任务失败"
        )
        executor.attach_browser(None)  # 不变式：executor 手里的要么是已启动的池，要么是 None
    try:
        return await run_all(
            provider=provider, executor=executor, tracer=tracer, teardown=executor.aclose
        )
    finally:
        if started:
            await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mock", action="store_true", help="run with fake deps (no network/tokens)"
    )
    args = parser.parse_args()

    tracer = JsonlTracer(out_dir=Path("data/traces"))
    if args.mock:
        return asyncio.run(
            run_all(provider=FakeProvider(), executor=FakeExecutor(), tracer=tracer)
        )
    return asyncio.run(_run_real(tracer))


if __name__ == "__main__":
    raise SystemExit(main())
