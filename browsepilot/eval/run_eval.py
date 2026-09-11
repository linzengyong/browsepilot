"""Eval runner (v0.3) — executes scenarios against the Tier1/2/3 pipeline.

Modes:
  --mock   : fake provider/executor (no network, no tokens) — CI mode
  default  : real model + services + browser pool (needs BP_MODEL_API_KEY) — acceptance mode

场景选择（P4-T2 的 skip 语义，schema 见 :mod:`eval.scenarios`）：

- ``mode`` 不匹配 → ``[SKIP]``，**不计入** pass/fail 计数、不影响 exit code。理由：mock 的
  FakeProvider/FakeExecutor 只伪造天气/搜索/抓页（**没有浏览器**），tier3/HITL/多轮场景在 mock 下
  是"跑不了"而不是"跑失败了"——把环境不匹配读成失败会污染成功率，而成功率是报告的核心数据。
- ``enabled=False`` → ``[DISABLED]``（列出但不跑）：P3 遗留的交互链
  （``t3_browser_interaction``：交互步尚未入图）。
- ``fragile=True`` 的场景失败照实打 FAIL，但**不计入** exit code（P4-D6：ddgs 内容漂移不是回归），
  汇总里单独计数，供报告引用。
- ``--tag`` 按 tag 过滤（T7 的 ``--tag acceptance``）；未知 tag 由 argparse 直接拒绝；
  选到 **0 条可运行场景**时非零退出 —— 空跑全绿与"跑过且全绿"在日志上无法区分，
  静默通过等于假通过（与 C1 同一条原则）。

场景的**执行/观测**整体在 :mod:`eval.harness`（P4-T3）：这里只做选择、打印与 exit code。
具体地，``_run_scenario`` 把一条场景交给 ``harness.run_scenario`` —— 它负责多轮（``follow_up``）
与三个注入字段：``outcome["tool_calls"]``（v2 的 ``AgentState`` 不写 ``execution_log``，所以
"图确实调过哪些工具"由 harness 从 ``call()`` 记录；否则 mock 下所有 ``tools_called`` 断言都是假失败）、
``outcome["token_usage"]``（从 ``ChatResult.usage`` 累加，拿不到就字段缺席）、
``outcome["duration_ms"]``（整场景墙钟）。字段名/来源见 ``eval/harness.py`` 的模块 docstring。

真实模式（P4 评测体系的前置接线，运维前置见 docs/v2-p3-report.md §十二「P4 评测前置（运维）」）：

- **注入浏览器池**：``AppToolExecutor`` 拿不到池时 6 个 Tier3 工具统一返回
  ``browser_unavailable`` —— tier3 场景会**全部**失败，而那是接线问题不是任务失败
  （P3 final review 给 P4 的关键输入）。所以 real 模式走同一个组合根：Settings →
  BrowserPool + AppToolExecutor（策略也从同一个 Settings 显式构造，I3），装配在
  ``harness.build_real_deps``；``_real_stack`` 只是保留给测试的薄壳（monkeypatch seam）。
  池起不来（本机没装 chromium rev / channel 写错）时打 WARNING 继续评测 Tier1/2：
  宁可让 tier3 的失败在报告里**可见地**归因到环境，也不要静默产出误导性的成功率。
- **逐场景隔离**（C2③）：每个场景跑完 ``executor.aclose()`` 把当前页还给池（``teardown``）。
  不隔离的话下一个场景的 ``browser_snapshot`` 可能拍到上一个场景留下的页面，报 completed
  却答非所问（C2b：未与任务目标匹配的快照不得算完成）。
- 池的生命周期跨场景（``harness.pool_lifecycle`` 的 start/close），**页面**的生命周期逐场景；
  因此 ``run_all`` 本身是 async（一个事件循环跑完全部场景）。

``finally`` 里跑 teardown：场景自己炸了（模型/服务异常）也要归还页面，否则脏页留给下一个场景；
而**下一个场景照跑** —— 单条异常只废自己（见 ``run_all`` 的逐场景 ``try/except``）。

报告（P4-D3/D4，实现在 :mod:`eval.report`）：

- ``--json <path>`` / ``--md <path>``：跑完把**整份**报告写出来（缺省不写，一次跑只产出日志）。
  路径带后缀 = 文件；结尾分隔符 / 已存在目录 / 无后缀 = 目录，写入 ``<目录>/<时间戳>.json|.md``
  （P4-D4 的 ``data/eval-reports/<timestamp>.json`` 与 P4-D7 的 ``--md data/eval-reports/`` 形状）。
- ``--repeat N``（P4-D7）：同一场景集重复 N 轮，报告含**每轮**结果 + 跨轮方差
  （成功率/延迟 min-max + stdev）与 flaky 标注。exit code 语义不变：**任何**一轮出现非 fragile
  失败就 gate（0/1 之外不新增码）。
- 空跑（``--tag`` 选不中任何可运行场景）不写报告：`run_all` 已经非零退出，没有数据可报。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

from browsepilot.agent.tracer import JsonlTracer, Tracer
from eval import harness, report
from eval.checks import run_check
from eval.fake_deps import FakeExecutor, FakeProvider
from eval.report import Round, ScenarioResult
from eval.scenarios import KNOWN_TAGS, Check, Scenario, load_scenarios

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


def check_outcome(outcome: dict, check: Check) -> tuple[bool, str]:
    """Evaluate one check against a run_task outcome dict. Returns (passed, detail).

    v0.3（P4-D1）：断言逻辑整体搬到 :mod:`eval.checks`（纯函数 + 全部检查类型）。
    这里保留 tuple 形状的薄适配层 —— ``run_all`` 与既有测试的调用方式不变；
    两个既有类型（field / contains_keyword）的通过语义也不变。
    """
    result = run_check(outcome, check)
    return result.passed, result.detail


async def _run_scenario(
    scenario: Scenario, *, provider, executor, tracer, teardown=None
) -> tuple[dict, list[tuple[bool, str]]]:
    """跑一个场景并评估 checks；``teardown``（real 模式 = ``executor.aclose``）在 finally 里跑。

    执行交给 :func:`eval.harness.run_scenario`（多轮 + ``tool_calls`` / ``token_usage`` /
    ``duration_ms`` 三个注入字段），这里只评估 checks 与归还页面。teardown 拿到的仍是**原**
    executor —— 归还页面是它的职责，跟记录无关。
    """
    try:
        outcome = await harness.run_scenario(
            scenario, provider=provider, executor=executor, tracer=tracer
        )
        checks = [check_outcome(outcome, c) for c in scenario.checks]
    finally:
        if teardown is not None:
            await teardown()
    return outcome, checks


@dataclass(frozen=True)
class ScenarioSelection:
    """一次运行对场景集的决策：要跑的 / 跳过的（带原因）/ 停用的。"""

    runnable: list[Scenario]
    skipped: list[tuple[Scenario, str]]
    disabled: list[Scenario]


def select_scenarios(
    scenarios: list[Scenario], *, mode: str, tags: list[str] | None = None
) -> ScenarioSelection:
    """按运行 ``mode`` 与 ``tags`` 过滤场景；停用（``enabled=False``）单独列出。

    判定次序：停用 → mode 不匹配 → tag 不匹配。取**最根本**的那条原因：停用是"声明了也暂不跑"，
    比"这次模式不匹配"更值得报出来 —— 否则 mock 的日志里看不出 tier3 交互场景为什么没跑。
    """
    if mode not in ("mock", "real"):
        raise ValueError(f"mode must be 'mock' or 'real', got {mode!r}")
    wanted = set(tags or ())
    runnable: list[Scenario] = []
    skipped: list[tuple[Scenario, str]] = []
    disabled: list[Scenario] = []
    for scenario in scenarios:
        if not scenario.enabled:
            disabled.append(scenario)
        elif scenario.mode not in ("both", mode):
            skipped.append((scenario, f"mode={scenario.mode}（本次运行 = {mode}）"))
        elif wanted and not wanted & set(scenario.tags):
            skipped.append((scenario, f"tags={scenario.tags} 不含 --tag {sorted(wanted)}"))
        else:
            runnable.append(scenario)
    return ScenarioSelection(runnable=runnable, skipped=skipped, disabled=disabled)


async def run_all(
    *,
    provider,
    executor,
    tracer: Tracer,
    teardown=None,
    mode: str = "mock",
    tags: list[str] | None = None,
    results: list[ScenarioResult] | None = None,
) -> int:
    """顺序跑选中的场景并打印逐场景结果；``teardown`` 在每个**跑过的**场景之后跑。

    跳过/停用的场景不跑也不 teardown（没取过页面就没有页面要还）。exit code 只由
    **非 fragile 的失败**决定（D6/D8：环境不匹配与内容漂移不该让门禁变红）。

    **逐场景隔离（C1，P4-D5）**：一条场景炸了（模型/服务异常、超时）不得中断整轮 ——
    捕获 ``Exception`` → 打 ``[FAIL] <id>: EXCEPTION <type>: <msg>`` → 计为失败 → 继续下一条。
    否则 T7 的三轮 acceptance 里一次 120s 卡顿就会把剩余场景全部作废（日志里既没有
    ``[FAIL]`` 行也没有汇总，看起来像"没跑过"而不是"跑挂了一条"）。异常折成一条失败断言，
    与 check 失败走**同一条**计数/报告路径（fragile 场景的异常同样不计入门禁）。

    ``results`` 是给报告层用的**收集器**：传了列表就按加载顺序追加每条场景的结果行
    （PASS/FAIL/SKIP/DISABLED 四种都记 —— 报告要能解释"为什么这条没跑"），返回码形状不变
    （既有调用方与测试都读 int）。
    """
    scenarios = load_scenarios(SCENARIOS_DIR)
    selection = select_scenarios(scenarios, mode=mode, tags=tags)
    scope = f", tags={sorted(tags)}" if tags else ""
    print(f"[browsepilot eval v0.3] {len(scenarios)} scenarios loaded (mode={mode}{scope})")
    if tags and not selection.runnable:
        # 空跑不得算通过（P4-T2 审查 Minor）：没有这条时下面会打印 passed=0 failed=0 并返回 0，
        # 与"跑过且全绿"在日志上无法区分（--tag 与 mode 的组合选错了也看不出来）。
        print(
            f"[eval] ERROR: --tag {sorted(tags)} 选中 0 条可运行场景"
            f"（skipped={len(selection.skipped)}, disabled={len(selection.disabled)}）"
            " —— 空跑不算通过"
        )
        return 1
    skip_reasons = {scenario.id: reason for scenario, reason in selection.skipped}
    disabled_ids = {scenario.id for scenario in selection.disabled}
    passed = failed = fragile_failed = 0
    for scenario in scenarios:
        common = {
            "id": scenario.id,
            "tier": scenario.tier,
            "tags": tuple(scenario.tags),
            "mode": mode,
            "fragile": scenario.fragile,
        }
        if scenario.id in disabled_ids:
            print(f"  [DISABLED] {scenario.id} ({scenario.name})")
            if results is not None:
                results.append(ScenarioResult(**common, disabled=True))
            continue
        reason = skip_reasons.get(scenario.id)
        if reason is not None:
            print(f"  [SKIP] {scenario.id}: {reason}")
            if results is not None:
                results.append(ScenarioResult(**common, skipped=True, skip_reason=reason))
            continue
        try:
            outcome, checks = await _run_scenario(
                scenario, provider=provider, executor=executor, tracer=tracer, teardown=teardown
            )
        except Exception as exc:
            # C1（P4-T3 审查，P4-D5）：单条场景炸了**不得**中断整轮 —— 记一条失败断言继续跑。
            # ``Exception`` 不含 ``CancelledError``（BaseException）：外部取消照常中止。
            # 没有可报的数据面（outcome = {}）——失败行本身就是证据；teardown 已在
            # _run_scenario 的 finally 里跑过（脏页不留给下一个场景）。
            outcome, checks = {}, [(False, f"EXCEPTION {type(exc).__name__}: {exc}")]
        ok = all(result for result, _ in checks)
        detail = "; ".join(text for _, text in checks)
        # 报告侧的失败明细只留**失败**的那几条（通过项不是"根因"）；采集不到的数字保持缺席（不填 0）。
        failures = "; ".join(text for passed_check, text in checks if not passed_check) or None
        usage = outcome.get("token_usage") or {}
        if results is not None:
            results.append(
                ScenarioResult(
                    **common,
                    passed=ok,
                    duration_ms=outcome.get("duration_ms"),
                    tokens=usage.get("total_tokens"),
                    cost_usd=usage.get("cost_usd"),
                    stop_reason=outcome.get("stop_reason"),
                    failure_detail=failures,
                )
            )
        if ok:
            passed += 1
            print(f"  [PASS] {scenario.id}: {detail}")
        elif scenario.fragile:
            fragile_failed += 1
            print(f"  [FAIL] {scenario.id}: {detail} (fragile=true — 不计入门禁 exit code)")
        else:
            failed += 1
            print(f"  [FAIL] {scenario.id}: {detail}")
    print(
        f"[eval] passed={passed} failed={failed} fragile_failed={fragile_failed} "
        f"skipped={len(selection.skipped)} disabled={len(selection.disabled)} "
        f"total={len(scenarios)}"
    )
    return 1 if failed else 0


def _write_reports(
    rounds: list[Round],
    *,
    mode: str,
    model: str | None,
    tags: list[str] | None,
    json_path: str | None,
    md_path: str | None,
) -> None:
    """把若干轮的结果落成 JSON/Markdown 报告（同一时间戳 → 两个文件名成对）。

    汇总口径：``summary`` = **跨轮样本池**（每轮各算一遍；多轮报告另给每轮表与方差，
    见 :mod:`eval.report`）。环境行带上模型 id 与 git HEAD 短号 —— 报告要能回答
    "这批数字是哪份代码、哪个模型、什么时候跑出来的"。
    """
    stamp = report.timestamp()
    pooled = [result for round_ in rounds for result in round_.results]
    aggregates = report.aggregate(pooled)
    env = report.environment(
        mode=mode,
        model=model,
        extra={"repeats": len(rounds), "tags": sorted(tags) if tags else None},
    )
    if json_path:
        target = report.write_json(
            aggregates, pooled, json_path, environment=env, rounds=rounds, stamp=stamp
        )
        print(f"[eval] report (JSON): {target}")
    if md_path:
        target = report.write_markdown(
            aggregates, pooled, md_path, environment=env, rounds=rounds, stamp=stamp
        )
        print(f"[eval] report (Markdown): {target}")


async def run_rounds(
    *,
    provider,
    executor,
    tracer: Tracer,
    mode: str,
    tags: list[str] | None = None,
    repeat: int = 1,
    teardown=None,
    json_path: str | None = None,
    md_path: str | None = None,
    model: str | None = None,
) -> int:
    """跑 ``repeat`` 轮同一场景集 → 汇总 → （可选）写报告；返回 exit code（0/1）。

    - 每轮都是**完整**的一遍（同一选择、同一顺序）：多轮的目的是量波动，不是抽样。
    - exit code 与单轮同语义：任何一轮出现非 fragile 失败就 gate（fragile 失败只计数）。
    - 空跑（``--tag`` 选不中可运行场景）时 ``run_all`` 返回 1 且一条结果都没有：不写报告、
      也不再重复 N 遍（没有数据可报，日志里已经报了原因）。
    """
    if repeat < 1:
        raise ValueError(f"repeat must be >= 1, got {repeat}")
    rounds: list[Round] = []
    exit_code = 0
    for index in range(1, repeat + 1):
        if repeat > 1:
            print(f"[eval] round {index}/{repeat}")
        results: list[ScenarioResult] = []
        exit_code = (
            await run_all(
                provider=provider,
                executor=executor,
                tracer=tracer,
                teardown=teardown,
                mode=mode,
                tags=tags,
                results=results,
            )
            or exit_code
        )
        if not results:  # 空跑：run_all 已经报错并返回 1
            break
        rounds.append(Round(index=index, results=results))
    if rounds and (json_path or md_path):
        _write_reports(
            rounds, mode=mode, model=model, tags=tags, json_path=json_path, md_path=md_path
        )
    return exit_code


def _real_stack():
    """真实模式的组合根（薄壳）：Settings → ``harness.build_real_deps``。

    保留这个函数的理由是**测试 seam**：既有测试 monkeypatch 它注入 fake pool/executor
    （真浏览器不进 pytest）。导入与装配都在 harness 里（``--mock`` 不该拉起
    litellm / playwright / ddgs 的依赖链）。
    """
    from browsepilot.app.config import Settings

    deps = harness.build_real_deps(Settings())
    return deps.provider, deps.executor, deps.pool


async def _run_real(
    tracer: Tracer,
    tags: list[str] | None = None,
    *,
    repeat: int = 1,
    json_path: str | None = None,
    md_path: str | None = None,
) -> int:
    """真实模式：起池 → 逐场景隔离评测 → 关池；池起不来就诚实降级（打警告，不静默）。

    池的生命周期在 :func:`eval.harness.pool_lifecycle` 里（含降级与异常安全）；``teardown``
    是逐场景的页面归还点（C2③）。池跨**全部轮次**只起一次（多轮之间不给模型换环境），
    否则 P4-D7 的"方差"里混进了池起停的噪声。模型 id 进环境行。
    """
    provider, executor, pool = _real_stack()
    async with harness.pool_lifecycle(pool, executor):
        return await run_rounds(
            provider=provider,
            executor=executor,
            tracer=tracer,
            teardown=executor.aclose,
            mode="real",
            tags=tags,
            repeat=repeat,
            json_path=json_path,
            md_path=md_path,
            model=getattr(provider, "model", None),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mock", action="store_true", help="run with fake deps (no network/tokens)"
    )
    parser.add_argument(
        "--tag",
        action="append",
        choices=list(KNOWN_TAGS),
        help=f"只跑带该 tag 的场景（可重复；缺省 = 全部；可选：{', '.join(KNOWN_TAGS)}）",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        metavar="PATH",
        help="机器可读报告写到该路径（目录 = 写入 <目录>/<时间戳>.json；缺省不写）",
    )
    parser.add_argument(
        "--md",
        dest="md_path",
        metavar="PATH",
        help="人读报告写到该路径（目录 = 写入 <目录>/<时间戳>.md；缺省不写）",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="同一场景集重复 N 轮（报告含每轮结果 + 跨轮方差；缺省 1）",
    )
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat 必须 >= 1（跑 0 轮不是一次评测，是空跑）")

    tracer = JsonlTracer(out_dir=Path("data/traces"))
    if args.mock:
        return asyncio.run(
            run_rounds(
                provider=FakeProvider(),
                executor=FakeExecutor(),
                tracer=tracer,
                mode="mock",
                tags=args.tag,
                repeat=args.repeat,
                json_path=args.json_path,
                md_path=args.md_path,
                model="mock (FakeProvider)",
            )
        )
    return asyncio.run(
        _run_real(
            tracer,
            tags=args.tag,
            repeat=args.repeat,
            json_path=args.json_path,
            md_path=args.md_path,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
