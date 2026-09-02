"""ModelProvider decorator that records llm_call trace events (article 5).

Wraps any ModelProvider so every LLM call (chat / stream / structured)
is recorded on the given Tracer as an ``llm_call`` TraceEvent carrying
kind, model, session_id and wall-clock duration — the agent's
observability story without touching the model layer.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from pydantic import BaseModel

from browsepilot.agent.tracer import TraceEvent, Tracer
from browsepilot.model.messages import ChatResult, Message
from browsepilot.model.provider import ModelProvider


class TracingModelProvider:
    """Drop-in ModelProvider wrapper: delegates calls, emits llm_call events."""

    def __init__(self, inner: ModelProvider, tracer: Tracer, session_id: str = ""):
        self._inner = inner
        self._tracer = tracer
        self._session_id = session_id

    async def chat(
        self, messages: list[Message], *, model: str | None = None
    ) -> ChatResult:
        start = time.monotonic()
        result = await self._inner.chat(messages, model=model)
        self._emit("chat", model, start)
        return result

    async def stream(
        self, messages: list[Message], *, model: str | None = None
    ) -> AsyncIterator[str]:
        start = time.monotonic()
        async for chunk in self._inner.stream(messages, model=model):
            yield chunk
        self._emit("stream", model, start)

    async def structured(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel],
        model: str | None = None,
        max_retries: int = 3,
    ) -> BaseModel:
        start = time.monotonic()
        result = await self._inner.structured(
            messages, schema=schema, model=model, max_retries=max_retries
        )
        self._emit("structured", model, start)
        return result

    def _emit(self, kind: str, model: str | None, start: float) -> None:
        self._tracer.emit(
            TraceEvent(
                trace_id=self._tracer.trace_id,
                span_id=self._tracer.new_span_id(),
                node="llm",
                event_type="llm_call",
                data={"kind": kind, "model": model or "", "session_id": self._session_id},
                duration_ms=(time.monotonic() - start) * 1000,
            )
        )
