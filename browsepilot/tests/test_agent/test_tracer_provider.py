"""TracingModelProvider: ModelProvider decorator recording llm_call events."""

import json

import pytest
from pydantic import BaseModel

from browsepilot.agent.tracer import JsonlTracer
from browsepilot.agent.tracer_provider import TracingModelProvider
from browsepilot.model.messages import ChatResult, Message


class _City(BaseModel):
    city: str


class _FakeInner:
    """Minimal ModelProvider stub whose calls get traced."""

    async def chat(self, messages, *, model=None):
        return ChatResult(text="hi", raw={})

    async def stream(self, messages, *, model=None):
        yield "h"
        yield "i"

    async def structured(self, messages, *, schema, model=None, max_retries=3):
        return schema(city="北京")


def _events(tracer) -> list[dict]:
    path = tracer.out_dir / f"{tracer.trace_id}.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _llm_call(tracer) -> dict:
    events = _events(tracer)
    llm = [e for e in events if e["event_type"] == "llm_call"]
    assert llm, f"no llm_call event in {events}"
    return llm[0]


@pytest.mark.asyncio
async def test_chat_emits_llm_call_event(tmp_path):
    tracer = JsonlTracer(trace_id="llm1", out_dir=tmp_path)
    p = TracingModelProvider(_FakeInner(), tracer, session_id="s1")
    result = await p.chat([Message(role="user", content="x")], model="m1")
    assert result.text == "hi"
    ev = _llm_call(tracer)
    assert ev["trace_id"] == "llm1"
    assert ev["node"] == "llm"
    assert ev["data"] == {"kind": "chat", "model": "m1", "session_id": "s1"}
    assert ev["duration_ms"] is not None


@pytest.mark.asyncio
async def test_structured_emits_llm_call_event(tmp_path):
    tracer = JsonlTracer(trace_id="llm2", out_dir=tmp_path)
    p = TracingModelProvider(_FakeInner(), tracer, session_id="s1")
    result = await p.structured(
        [Message(role="user", content="x")], schema=_City, model="m2"
    )
    assert result.city == "北京"
    ev = _llm_call(tracer)
    assert ev["data"]["kind"] == "structured"
    assert ev["data"]["model"] == "m2"
    assert ev["data"]["session_id"] == "s1"
    assert ev["duration_ms"] is not None


@pytest.mark.asyncio
async def test_stream_emits_llm_call_event_after_drain(tmp_path):
    tracer = JsonlTracer(trace_id="llm3", out_dir=tmp_path)
    p = TracingModelProvider(_FakeInner(), tracer, session_id="s1")
    chunks = [c async for c in p.stream([Message(role="user", content="x")])]
    assert chunks == ["h", "i"]
    ev = _llm_call(tracer)
    assert ev["data"]["kind"] == "stream"
    assert ev["data"]["model"] == ""
    assert ev["data"]["session_id"] == "s1"
    assert ev["duration_ms"] is not None
