"""V2 FastAPI application factory — the composition root."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from browsepilot import __version__
from browsepilot.agent.api import resume_task, run_task
from browsepilot.agent.tools import ToolExecutor
from browsepilot.agent.tracer import Tracer
from browsepilot.app.config import Settings
from browsepilot.app.sessions import SessionInfo, SessionStore
from browsepilot.model.provider import ModelProvider


class ChatRequest(BaseModel):
    task: str = Field(min_length=1)
    session_id: str = ""


class ResumeRequest(BaseModel):
    """POST /chat/resume：恢复被 HITL interrupt 暂停的任务。"""

    session_id: str = Field(min_length=1)
    human_response: str = Field(min_length=1)


class RenameRequest(BaseModel):
    name: str = Field(min_length=1)


class OkResponse(BaseModel):
    ok: bool = True


class PinResponse(BaseModel):
    pinned: bool


class TurnRecord(BaseModel):
    """一轮对话的持久化记录（/history 返回单元）。"""

    task: str
    answer: str
    created_at: str


class HistoryResponse(BaseModel):
    session: SessionInfo
    turns: list[TurnRecord]


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


_HISTORY_MAX_ENTRIES = 5
_HISTORY_MAX_ENTRY_CHARS = 200


def _build_history_entries(turns: list[dict]) -> list[str]:
    """Turns 行 → AgentState.history 摘要条目（多轮上下文）。

    每条格式 "Q: <task> / A: <answer>"，压成单行、截断 ~200 字符；
    只保留最近 5 条（answer 节点按 [-5:] 兜底同样限制）。
    """
    entries: list[str] = []
    for turn in turns[-_HISTORY_MAX_ENTRIES:]:
        task = (turn.get("task") or "").replace("\n", " ").strip()
        answer = (turn.get("answer") or "").replace("\n", " ").strip()
        entry = f"Q: {task} / A: {answer}"
        entries.append(entry[:_HISTORY_MAX_ENTRY_CHARS])
    return entries


def create_app(
    settings: Settings | None = None,
    *,
    provider: ModelProvider,
    executor: ToolExecutor,
    tracer: Tracer,
    session_store: SessionStore | None = None,
    checkpointer=None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("browsepilot v2.0 app starting (model={})", settings.default_model)
        yield
        logger.info("browsepilot app shutting down")

    app = FastAPI(
        title="BrowsePilot API",
        description="AI 驱动的信息获取 Agent。Tier1 搜索/天气走纯 API，Tier2 网页正文提取，Tier3 浏览器交互（P3 上线）。",
        version=__version__,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__, "model": settings.default_model}

    # ── Session metadata CRUD ──────────────────────────────────────────────
    # Endpoints run on the injected store; a store-less app (only possible in
    # direct unit calls) answers 503 instead of crashing.
    def _store() -> SessionStore:
        if session_store is None:
            raise HTTPException(status_code=503, detail="session store is not configured")
        return session_store

    @app.get("/sessions", response_model=list[SessionInfo])
    def list_sessions():
        """List sessions — pinned first, then most recently updated."""
        return _store().list_sessions()

    @app.patch("/sessions/{session_id}/rename", response_model=OkResponse)
    def rename_session(session_id: str, body: RenameRequest):
        """Rename a session."""
        _store().rename_session(session_id, body.name)
        return OkResponse()

    @app.patch("/sessions/{session_id}/pin", response_model=PinResponse)
    def toggle_pin(session_id: str):
        """Toggle a session's pinned state; returns the new state."""
        return PinResponse(pinned=_store().toggle_pin(session_id))

    @app.delete("/sessions/{session_id}", response_model=OkResponse)
    def delete_session(session_id: str):
        """Delete a session (idempotent)."""
        _store().delete_session(session_id)
        return OkResponse()

    @app.get("/history/{session_id}", response_model=HistoryResponse)
    def history(session_id: str):
        """List a session's recorded turns (task/answer/created_at, in order)."""
        store = _store()
        info = store.get_session(session_id)
        if info is None:
            raise HTTPException(status_code=404, detail="session not found")
        return {"session": info.model_dump(), "turns": store.list_turns(session_id)}

    @app.post("/chat/stream")
    async def chat_stream(req: ChatRequest):
        """执行一个用户任务，SSE 返回阶段事件与最终回答。

        session_id 非空且配置了 session_store 时：把该会话已记录的轮次
        格式化为历史摘要注入模型上下文（多轮）；任务完成后把本轮
        task/answer 记录进 turns 表（持久化失败不破坏 SSE 契约）。
        """

        def _history_context() -> list[str]:
            if not req.session_id or session_store is None:
                return []
            return _build_history_entries(session_store.list_turns(req.session_id))

        async def gen():
            yield _sse({"type": "phase", "phase": "start", "task": req.task[:100]})
            try:
                result = await run_task(
                    req.task,
                    provider=provider,
                    executor=executor,
                    tracer=tracer,
                    session_id=req.session_id,
                    history=_history_context(),
                    checkpointer=checkpointer,
                    config=(
                        {"configurable": {"thread_id": req.session_id}}
                        if req.session_id
                        else None
                    ),
                )
            except Exception as exc:
                logger.exception("chat stream failed for task {!r}", req.task[:100])
                yield _sse({"type": "error", "message": f"任务执行失败：{exc}"})
                yield _sse({"type": "done", "stop_reason": "error", "result_data": {}})
                return
            if req.session_id and session_store is not None:
                try:
                    session_store.record_turn(
                        req.session_id, req.task, result.get("final_answer", "")
                    )
                except Exception:
                    logger.exception("record_turn failed for session {!r}", req.session_id)
            yield _sse({"type": "answer", "text": result.get("final_answer", "")})
            yield _sse(
                {
                    "type": "done",
                    "stop_reason": result.get("stop_reason", ""),
                    "result_data": result.get("result_data", {}),
                }
            )

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/chat/resume")
    async def chat_resume(req: ResumeRequest):
        """恢复被 HITL interrupt 暂停的任务，SSE 返回最终回答（无 phase/start）。

        校验失败（无待处理 interrupt）时发 error/done，不静默开新任务。
        """

        async def gen():
            try:
                result = await resume_task(
                    req.session_id,
                    req.human_response,
                    provider=provider,
                    executor=executor,
                    tracer=tracer,
                    checkpointer=checkpointer,
                )
            except Exception as exc:
                logger.exception("chat resume failed for session {!r}", req.session_id)
                yield _sse({"type": "error", "message": f"恢复任务失败：{exc}"})
                yield _sse({"type": "done", "stop_reason": "error", "result_data": {}})
                return
            yield _sse({"type": "answer", "text": result.get("final_answer", "")})
            yield _sse(
                {
                    "type": "done",
                    "stop_reason": result.get("stop_reason", ""),
                    "result_data": result.get("result_data", {}),
                }
            )

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def _default_app() -> FastAPI:
    """Assemble the production wiring from Settings (BP_ env vars / .env)."""
    from pathlib import Path

    from browsepilot.agent.tracer import JsonlTracer
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
    tracer = JsonlTracer(out_dir=Path(settings.trace_dir))
    session_store = SessionStore(Path(settings.session_db_path))
    return create_app(
        settings,
        provider=provider,
        executor=executor,
        tracer=tracer,
        session_store=session_store,
    )


app = _default_app()
