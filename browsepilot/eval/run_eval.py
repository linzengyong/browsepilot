"""Eval runner (v0.2) — executes scenarios against the Tier1/2 pipeline.

Modes:
  --mock   : fake provider/executor (no network, no tokens) — CI mode
  default  : real model + services (needs BP_MODEL_API_KEY) — acceptance mode
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from browsepilot.agent.api import run_task
from browsepilot.agent.tracer import JsonlTracer
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
    scenario: Scenario, *, provider, executor, tracer
) -> tuple[dict, list[tuple[bool, str]]]:
    outcome = await run_task(scenario.prompt, provider=provider, executor=executor, tracer=tracer)
    return outcome, [check_outcome(outcome, c) for c in scenario.checks]


def run_all(*, provider, executor, tracer) -> int:
    scenarios = load_scenarios(SCENARIOS_DIR)
    print(f"[browsepilot eval v0.2] {len(scenarios)} scenarios loaded")
    failed = 0
    for s in scenarios:
        outcome, checks = asyncio.run(
            _run_scenario(s, provider=provider, executor=executor, tracer=tracer)
        )
        ok = all(passed for passed, _ in checks)
        failed += 0 if ok else 1
        detail = "; ".join(d for _, d in checks)
        print(f"  [{'PASS' if ok else 'FAIL'}] {s.id}: {detail}")
    print(f"[eval] {len(scenarios) - failed}/{len(scenarios)} passed")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mock", action="store_true", help="run with fake deps (no network/tokens)"
    )
    args = parser.parse_args()

    if args.mock:
        provider, executor = FakeProvider(), FakeExecutor()
    else:
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
        executor = AppToolExecutor(WeatherService(), SearchService(), PageFetchService())

    tracer = JsonlTracer(out_dir=Path("data/traces"))
    return run_all(provider=provider, executor=executor, tracer=tracer)


if __name__ == "__main__":
    raise SystemExit(main())
