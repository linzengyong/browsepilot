"""Tracer port + JSONL sink — the agent's black-box recorder (article 5).

The agent core only knows "record events". Where they go (file, Langfuse,
LangSmith) is a pluggable sink. V2 default: JSONL files.

Span semantics: one span owns ONE span_id, shared by its node_start and
node_end events. Nesting is tracked per-tracer via a contextvar stack
(task-local, async-safe), so concurrent graph runs on the same tracer
instance don't corrupt parent linkage.
"""

from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class TraceEvent(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    node: str = ""
    event_type: str = (
        "info"  # node_start | node_end | tool_call | llm_call | decision | error | info
    )
    timestamp: str = Field(default_factory=_now_iso)
    data: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = None


@runtime_checkable
class Tracer(Protocol):
    trace_id: str

    def emit(self, event: TraceEvent) -> None: ...

    def span(self, node: str, **data: Any) -> AbstractContextManager[TraceSpan]: ...

    def new_span_id(self) -> str: ...


class TraceSpan:
    """One instrumented span: emits node_start on enter, node_end on exit."""

    def __init__(
        self,
        tracer: Tracer,
        node: str,
        *,
        parent_span_id: str | None = None,
        stack: contextvars.ContextVar | None = None,
        **data: Any,
    ):
        self._tracer = tracer
        self._node = node
        self._data = data
        self._span_id = tracer.new_span_id()
        self._parent_span_id = parent_span_id
        self._stack = stack
        self._stack_token: contextvars.Token | None = None
        self._start: float | None = None

    def __enter__(self) -> TraceSpan:
        self._start = time.monotonic()
        if self._stack is not None:
            current = self._stack.get()
            self._stack_token = self._stack.set(current + (self._span_id,))
        self._tracer.emit(
            TraceEvent(
                trace_id=self._tracer.trace_id,
                span_id=self._span_id,
                parent_span_id=self._parent_span_id,
                node=self._node,
                event_type="node_start",
                data=self._data,
            )
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._stack is not None and self._stack_token is not None:
            self._stack.reset(self._stack_token)
        self._tracer.emit(
            TraceEvent(
                trace_id=self._tracer.trace_id,
                span_id=self._span_id,
                parent_span_id=self._parent_span_id,
                node=self._node,
                event_type="node_end",
                duration_ms=(time.monotonic() - (self._start or 0.0)) * 1000,
            )
        )


class JsonlTracer:
    """Append-only JSONL tracer, one file per trace_id."""

    def __init__(self, trace_id: str | None = None, out_dir: Path = Path("data/traces")):
        self.trace_id = trace_id or str(uuid.uuid4())
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.out_dir / f"{self.trace_id}.jsonl"
        self._lock = threading.Lock()
        # Per-instance stack of active span ids; contextvars make it
        # task-local so concurrent async runs don't interleave.
        self._span_stack: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
            f"bp_span_stack_{id(self)}", default=()
        )

    def new_span_id(self) -> str:
        return str(uuid.uuid4())[:8]

    def emit(self, event: TraceEvent) -> None:
        line = json.dumps(event.model_dump(), ensure_ascii=False)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def span(self, node: str, **data: Any) -> TraceSpan:
        stack = self._span_stack.get()
        parent = stack[-1] if stack else None
        return TraceSpan(self, node, parent_span_id=parent, stack=self._span_stack, **data)
