import json
from pathlib import Path

from browsepilot.agent.tracer import JsonlTracer, Tracer, TraceEvent


def test_tracer_implements_protocol():
    assert isinstance(JsonlTracer(trace_id="abc", out_dir=Path("data/traces")), Tracer)


def test_emit_writes_jsonl_line(tmp_path):
    tracer = JsonlTracer(trace_id="abc", out_dir=tmp_path)
    tracer.emit(
        TraceEvent(trace_id="abc", span_id="s1", node="x", event_type="info", data={"k": 1})
    )
    lines = (tmp_path / "abc.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["node"] == "x"
    assert ev["data"]["k"] == 1
    assert ev["event_type"] == "info"


def test_span_emits_start_and_end(tmp_path):
    tracer = JsonlTracer(trace_id="abc", out_dir=tmp_path)
    with tracer.span("classify", task="hi"):
        pass
    events = [
        json.loads(line)
        for line in (tmp_path / "abc.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ]
    assert [e["event_type"] for e in events] == ["node_start", "node_end"]
    assert events[0]["node"] == "classify"
    assert events[0]["data"]["task"] == "hi"
    assert events[1]["duration_ms"] is not None
