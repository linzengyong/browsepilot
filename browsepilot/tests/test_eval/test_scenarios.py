from pathlib import Path

import pytest
from pydantic import ValidationError

import eval.scenarios as es
from eval.run_eval import main

SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent / "eval" / "scenarios"


def test_scenario_files_load_and_validate():
    scenarios = es.load_scenarios(SCENARIOS_DIR)
    ids = {s.id for s in scenarios}
    assert {"p0-weather", "p0-search", "p0-read-page"} <= ids
    assert all(s.prompt for s in scenarios)
    assert all(s.tier in ("tier1", "tier2", "tier3") for s in scenarios)


def test_scenario_requires_prompt():
    with pytest.raises(ValidationError):
        es.Scenario(id="x", name="x")


def test_run_eval_smoke(capsys):
    assert main() == 0
    out = capsys.readouterr().out
    assert "scenarios loaded" in out
    assert "p0-weather" in out
