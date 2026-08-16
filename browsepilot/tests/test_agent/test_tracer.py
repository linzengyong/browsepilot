import json
from pathlib import Path

from browsepilot.agent.tracer import JsonlTracer, TraceEvent, Tracer


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


def test_span_start_and_end_share_span_id(tmp_path):
    tracer = JsonlTracer(trace_id="abc", out_dir=tmp_path)
    with tracer.span("classify", task="hi"):
        pass
    events = [
        json.loads(line)
        for line in (tmp_path / "abc.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ]
    assert events[0]["span_id"] == events[1]["span_id"]
    assert events[0]["parent_span_id"] is None


def test_nested_spans_link_parent_ids(tmp_path):
    tracer = JsonlTracer(trace_id="abc", out_dir=tmp_path)
    with tracer.span("outer"):
        with tracer.span("inner"):
            pass
    events = [
        json.loads(line)
        for line in (tmp_path / "abc.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ]
    outer_start, inner_start, inner_end, outer_end = events
    assert [e["node"] for e in events] == ["outer", "inner", "inner", "outer"]
    assert inner_start["parent_span_id"] == outer_start["span_id"]
    assert inner_end["span_id"] == inner_start["span_id"]
    assert outer_start["parent_span_id"] is None


def test_sibling_spans_have_no_parent_after_exit(tmp_path):
    tracer = JsonlTracer(trace_id="abc", out_dir=tmp_path)
    with tracer.span("a"):
        pass
    with tracer.span("b"):
        pass
    events = [
        json.loads(line)
        for line in (tmp_path / "abc.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ]
    a_start, a_end, b_start, b_end = events
    assert a_start["parent_span_id"] is None
    assert b_start["parent_span_id"] is None
