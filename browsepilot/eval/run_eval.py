"""Eval runner skeleton (v0.1).

P0: loads and validates scenarios, prints PENDING.
P1: executes scenarios against the Tier1/2 pipeline and reports pass/fail.
"""
from __future__ import annotations

from pathlib import Path

from eval.scenarios import load_scenarios

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


def main() -> int:
    scenarios = load_scenarios(SCENARIOS_DIR)
    print(f"[browsepilot eval v0.1] {len(scenarios)} scenarios loaded")
    for s in scenarios:
        print(f"  - [{s.tier}] {s.id}: {s.name} — PENDING (execution in P1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
