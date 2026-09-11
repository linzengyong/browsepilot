"""``eval/bench.py`` 的契约测试（P5-T3）：离线可跑 + 口径一致 + 无凭据明确 SKIP。

bench 是**测量脚本**（不发断言、不进 ``eval/gate.sh``），但它有三条不能破的契约：

1. 分位数口径与 :mod:`eval.report` 一致 —— 否则 ``docs/v2-performance.md`` 的数字和 T7 报告的
   数字不是同一把尺子（P50/P95 都读不了）；
2. mock 段**离线**可跑且跑出 ``completed`` —— 文档里的"噪声基线"必须来自真的跑通的管线，
   不是"跑了个空壳"；
3. 无凭据时 real 段 **SKIP**（而不是抛错）—— "没 key 就只跑 mock 并记录 SKIP" 是 P5-T3 的
   明确口径；同时 ``OPENAI_API_KEY`` → ``BP_MODEL_API_KEY`` 的进程内映射必须真的发生
   （litellm 对 ``deepseek/`` 前缀不会自己回退读 OPENAI_API_KEY，Settings 也只读 ``BP_``）。
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from eval import bench


def test_summarize_matches_report_percentile():
    """线性插值口径：4 个样本的 P50 = (20+30)/2，P95 = 30 + 0.85*10。"""
    stats = bench.summarize([10.0, 20.0, 30.0, 40.0])
    assert stats["n"] == 4
    assert stats["p50_ms"] == 25.0
    assert stats["p95_ms"] == 38.5
    assert stats["min_ms"] == 10.0
    assert stats["max_ms"] == 40.0


def test_summarize_rejects_empty():
    """"没有样本"不是 0ms：宁可报错，也不产出可被误读的读数。"""
    with pytest.raises(ValueError):
        bench.summarize([])


def test_section_json_keeps_raw_samples():
    section = bench.Section("demo")
    section.record("duration_ms", 12.345)
    section.record("duration_ms", 20.0)
    payload = section.to_json()
    assert payload["name"] == "demo"
    assert payload["status"] == "ok"
    assert payload["metrics"]["duration_ms"]["n"] == 2
    assert payload["samples_ms"]["duration_ms"] == [12.35, 20.0]


def test_real_credentials_maps_openai_key_into_bp_namespace(monkeypatch):
    """T7 §7.1 的口径固化：OPENAI_API_KEY/BASE_URL → BP_MODEL_API_KEY/BASE_URL（进程内）。"""
    monkeypatch.setattr(bench, "Settings", lambda: SimpleNamespace(model_api_key=""))
    monkeypatch.delenv("BP_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("BP_MODEL_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")

    available, note = bench.real_credentials()

    assert available is True
    assert "OPENAI_API_KEY" in note
    assert os.environ["BP_MODEL_API_KEY"] == "sk-test"
    assert os.environ["BP_MODEL_BASE_URL"] == "https://example.invalid/v1"


def test_real_credentials_skips_without_any_key(monkeypatch):
    """无 key → SKIP（返回 False + 原因），不抛 —— mock 段还得照跑。"""
    monkeypatch.setattr(bench, "Settings", lambda: SimpleNamespace(model_api_key=""))
    monkeypatch.delenv("BP_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    available, note = bench.real_credentials()

    assert available is False
    assert "SKIP" in note


def test_timed_subprocess_times_a_fresh_interpreter(tmp_path):
    """冷启动测量必须换进程：这条探针本身就是新解释器，耗时 > 0。"""
    assert bench.timed_subprocess([sys.executable, "-c", "pass"], cwd=tmp_path) > 0


def test_health_section_is_ok_offline(tmp_path):
    """``/health`` 段离线可跑、返回 200、样本数 = repeat。"""
    section = bench.measure_health(2, tmp_path)
    assert section.status == "ok"
    assert section.note == ""
    assert len(section.series["GET /health"]) == 2
    assert section.extra["body"]["status"] == "ok"


def test_mock_e2e_section_runs_offline_and_completes(tmp_path):
    """mock 端到端（噪声基线）离线可跑：2 次、stop_reason=completed、耗时 > 0。"""
    section = bench.measure_mock_e2e(2, bench.load_scenario(), tmp_path)
    assert section.status == "ok"
    assert section.note == ""
    assert len(section.series["duration_ms"]) == 2
    assert all(value > 0 for value in section.series["duration_ms"])
    assert section.extra["stop_reason"] == ["completed"]
    assert section.extra["tools_called"] == ["weather"]


def test_main_records_skips_and_real_failures_without_losing_mock_sections(monkeypatch, tmp_path):
    """exit code 与分节状态：无故障 = 0；real 整体炸 = 1（读数不合格）且 mock 读数照留。"""
    monkeypatch.setattr(bench, "measure_cold_start", lambda repeat: bench.Section("1. cold start"))
    monkeypatch.setattr(
        bench, "measure_create_app", lambda repeat, trace_dir: bench.Section("2. create_app")
    )
    monkeypatch.setattr(bench, "measure_health", lambda repeat, trace_dir: bench.Section("3. /health"))
    monkeypatch.setattr(
        bench, "measure_mock_e2e", lambda repeat, scenario, trace_dir: bench.Section("4. mock")
    )
    monkeypatch.setattr(bench, "environment", lambda *args, **kwargs: {"date": "test-run"})
    monkeypatch.setattr(bench, "real_credentials", lambda: (True, "测试凭据"))

    def read_sections(path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        by_name = {section["name"]: section for section in payload["sections"]}
        real = next(name for name in by_name if name.startswith("5. tier1 e2e real"))
        return by_name, real

    # ① --skip-real：正常退出（0），real 段如实记 skipped
    skipped_json = tmp_path / "skip.json"
    assert bench.main(["--repeat", "1", "--skip-real", "--json", str(skipped_json)]) == 0
    by_name, real = read_sections(skipped_json)
    assert by_name["4. mock"]["status"] == "ok"
    assert by_name[real]["status"] == "skipped"

    # ② real 段整体炸（如 build_real_deps 报错）：mock 读数照留，real 记 failed + 原因，exit 1
    def boom(repeat, scenario, trace_dir, *, note=""):
        raise RuntimeError("no browser / bad settings")

    monkeypatch.setattr(bench, "measure_real_e2e", boom)
    boom_json = tmp_path / "boom.json"
    assert bench.main(["--repeat", "1", "--json", str(boom_json)]) == 1
    by_name, real = read_sections(boom_json)
    assert by_name["4. mock"]["status"] == "ok"
    assert by_name[real]["status"] == "failed"
    assert "RuntimeError" in by_name[real]["note"]
