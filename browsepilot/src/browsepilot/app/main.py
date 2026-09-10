"""V2 FastAPI application factory — the composition root."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import aiosqlite
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from loguru import logger
from pydantic import BaseModel, Field

from browsepilot import __version__
from browsepilot.agent.api import has_pending_human_action, resume_task, run_task
from browsepilot.agent.state import AgentState, ExecutionStep
from browsepilot.agent.tools import ToolExecutor
from browsepilot.agent.tracer import Tracer
from browsepilot.app.config import Settings
from browsepilot.app.rate_limit import SlidingWindowRateLimiter
from browsepilot.app.sessions import SessionInfo, SessionStore
from browsepilot.model.provider import ModelProvider

if TYPE_CHECKING:
    from browsepilot.app.browser.browser_pool import BrowserPool


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


# ── P2 生产安全层 ────────────────────────────────────────────────────────
# 受限流的生成型 chat 端点（按客户端 IP 计数）。
_RATE_LIMITED_PATHS = ("/chat/stream", "/chat/resume")
# 会话预算估算：每个已记录 turn 的粗粒度 token 上限（含注入历史 + 回答生成）。
# 估算性质 —— P4 换精确 token 统计。
_EST_TOKENS_PER_TURN = 4000

# ── Checkpoint serde msgpack 白名单（P2 MUST-FIX）──────────────────────────
# lifespan 手动构造 AsyncSqliteSaver 时显式传入 serde：langgraph-checkpoint 4.x
# 的 JsonPlusSerializer 默认（allowed_msgpack_modules=True）对未注册类型只 warn
# 放行，严格模式（LANGGRAPH_STRICT_MSGPACK=true）下则直接拒 → resume 读回裸 dict。
# 允许项取类型对象本身（归一化成 (module, name) 键）；框架自有类型
# （PregelTask/Send/Interrupt 等）在 SAFE_MSGPACK_TYPES 中，无需重复列举。
_MSGPACK_ALLOWED_TYPES: tuple[type, ...] = (AgentState, ExecutionStep)


def _bind_executor_browser(executor: ToolExecutor, browser: BrowserPool | None) -> None:
    """把池绑定到 executor 的浏览器槽位（未接线 / 启动失败 → ``None``）。

    T4 给 AppToolExecutor 加了可选的 ``browser`` 构造参数，但池的 ``start()`` 是异步的：
    只有 lifespan 知道池是否真的可用。这里保证一个不变式 —— lifespan 启动完成后，
    executor 持有的要么是已启动的池，要么是 None（6 个浏览器工具统一返回
    ``browser_unavailable``），绝不会拿着一个没启动的池。非 AppToolExecutor 的
    ToolExecutor 替身没有该槽位时静默跳过。
    """
    if hasattr(executor, "_browser"):
        executor._browser = browser  # type: ignore[attr-defined]


def create_app(
    settings: Settings | None = None,
    *,
    provider: ModelProvider,
    executor: ToolExecutor,
    tracer: Tracer,
    session_store: SessionStore | None = None,
    checkpointer=None,
    rate_limiter: SlidingWindowRateLimiter | None = None,
    browser_pool: BrowserPool | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # P3 Tier3 接线：BrowserPool 的 start/close 归 lifespan。start() 失败 → 记日志、
        # executor 的 browser 置 None（6 个浏览器工具统一 browser_unavailable），app 照常
        # 启动 —— 诚实降级，不为浏览器故障崩服务。传 None（测试 / Tier3 关闭）则完全跳过。
        if browser_pool is not None:
            try:
                await browser_pool.start()
            except Exception:
                logger.exception(
                    "BrowserPool.start() failed; Tier3 degraded (browser=None), app continues"
                )
                _bind_executor_browser(executor, None)
            else:
                _bind_executor_browser(executor, browser_pool)
        try:
            # H1：生产接线——未显式传 checkpointer 时按 settings.checkpoint_db_path
            # 创建共享 AsyncSqliteSaver（跨请求存活），shutdown 时关闭底层连接。
            # 显式传入（测试）的 checkpointer 由调用方管理生命周期。
            # 注意：from_conn_string 不接受 serde，故手动构造 conn + saver（serde 见
            # _MSGPACK_ALLOWED_TYPES 的 msgpack 白名单注册）。
            if checkpointer is None and settings.checkpoint_db_path:
                db_path = Path(settings.checkpoint_db_path)
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(str(db_path))
                try:
                    saver = AsyncSqliteSaver(
                        conn,
                        serde=JsonPlusSerializer(allowed_msgpack_modules=_MSGPACK_ALLOWED_TYPES),
                    )
                    await saver.setup()
                    app.state.checkpointer = saver
                    logger.info(
                        "browsepilot v2.0 app starting (model={})", settings.default_model
                    )
                    yield
                finally:
                    await conn.close()
                    logger.info("browsepilot app shutting down (checkpointer closed)")
            else:
                logger.info("browsepilot v2.0 app starting (model={})", settings.default_model)
                yield
                logger.info("browsepilot app shutting down")
        finally:
            # 启动失败也 close()：池未启动时是幂等空操作；start() 失败路径的清理由
            # BrowserPool.start 自己负责（_discard_playwright）。
            if browser_pool is not None:
                await browser_pool.close()
                logger.info("browsepilot app shutting down (browser pool closed)")

    app = FastAPI(
        title="BrowsePilot API",
        description="AI 驱动的信息获取 Agent。Tier1 搜索/天气走纯 API，Tier2 网页正文提取，Tier3 浏览器交互（P3 上线）。",
        version=__version__,
        lifespan=lifespan,
    )

    # ── Rate limiting middleware（P2 生产安全层）───────────────────────────
    # 内存滑动窗口（单实例；scale 时换 Redis 实现）。只保护生成型 chat 端点，
    # 按客户端 IP 限流：超限 → 429 + Retry-After（窗口秒数）。create_app 默认
    # 不注入 limiter（单测旁路）；生产接线 _default_app 按 Settings 构造。
    @app.middleware("http")
    async def rate_limit_middleware(request, call_next):
        if rate_limiter is not None and request.url.path in _RATE_LIMITED_PATHS:
            client = request.client.host if request.client else "unknown"
            if not rate_limiter.allow(client):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "rate limit exceeded"},
                    headers={"Retry-After": str(settings.rate_limit_window_seconds)},
                )
        return await call_next(request)

    def _active_checkpointer():
        """请求期实际使用的 checkpointer：优先显式传入，否则 lifespan 创建的。"""
        return checkpointer or getattr(app.state, "checkpointer", None)

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

        配置了 session_store 时：会话行自动创建 —— 新会话（含空 session_id 的
        匿名首聊）在请求开始时落 sessions 行（行 id == effective_session_id，
        即 phase/start 公布的 id），客户端凭它对 /chat/resume、/history 寻址；
        再把该会话已记录的轮次格式化为历史摘要注入模型上下文（多轮）；任务
        完成后把本轮 task/answer 记录进 turns 表（持久化失败不破坏 SSE 契约）。

        P2 预算闸门：已记录 turns 数 × 4000 估算会话累计 token 开销，超
        session_budget_tokens → 429（估算性质，P4 换精确 token 统计）。
        """
        # Effective session/thread id —— 匿名会话由服务端生成 uuid4，并在
        # phase/start 与 human_pending 事件中公布，客户端才能凭它寻址
        # /chat/resume（否则匿名会话的 HITL interrupt 不可恢复）。
        effective_session_id = req.session_id or str(uuid4())

        # 会话行自动创建：任何会话（含匿名首轮）第一次 chat 时落行，
        # GET /sessions 与 /history 从第一轮起一致可见。必须先于预算检查 ——
        # 预算按该会话已记录 turns 估算，行存在时两者口径一致。
        if (
            session_store is not None
            and session_store.get_session(effective_session_id) is None
        ):
            session_store.create_session(
                effective_session_id, session_id=effective_session_id
            )

        # 预算检查必须在启动任务前完成（此处 429 返回 JSON，而非 SSE 错误流）
        if session_store is not None:
            turn_count = len(session_store.list_turns(effective_session_id))
            estimated = turn_count * _EST_TOKENS_PER_TURN
            if estimated > settings.session_budget_tokens:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"session token budget exceeded: {turn_count} turns × "
                        f"{_EST_TOKENS_PER_TURN} ≈ {estimated} tokens > budget "
                        f"{settings.session_budget_tokens}; start a new session"
                    ),
                )

        def _history_context() -> list[str]:
            if session_store is None:
                return []
            return _build_history_entries(session_store.list_turns(effective_session_id))

        async def gen():
            yield _sse(
                {
                    "type": "phase",
                    "phase": "start",
                    "task": req.task[:100],
                    "session_id": effective_session_id,
                }
            )
            try:
                # H1：空 session_id 时服务端生成 uuid4 thread_id，不依赖
                # LangGraph 内部随机（避免不可寻址的孤儿 checkpoint）。
                config = {"configurable": {"thread_id": effective_session_id}}
                if req.session_id and await has_pending_human_action(
                    effective_session_id,
                    provider=provider,
                    executor=executor,
                    tracer=tracer,
                    checkpointer=_active_checkpointer(),
                ):
                    # H4：线程已暂停在 HITL interrupt —— 拒绝新任务，不静默 resume
                    yield _sse(
                        {
                            "type": "error",
                            "message": "session has a pending human action; call /chat/resume",
                        }
                    )
                    yield _sse({"type": "done", "stop_reason": "error", "result_data": {}})
                    return
                result = await run_task(
                    req.task,
                    provider=provider,
                    executor=executor,
                    tracer=tracer,
                    session_id=effective_session_id,
                    history=_history_context(),
                    checkpointer=_active_checkpointer(),
                    config=config,
                )
            except Exception as exc:
                logger.exception("chat stream failed for task {!r}", req.task[:100])
                yield _sse({"type": "error", "message": f"任务执行失败：{exc}"})
                yield _sse({"type": "done", "stop_reason": "error", "result_data": {}})
                return
            if (
                not result.get("final_answer")
                and (result.get("__interrupt__") or result.get("pending_human_action"))
            ):
                # H2：HITL 暂停 —— 不发空 answer/done、不落库；告知客户端转 /chat/resume
                yield _sse(
                    {
                        "type": "human_pending",
                        "session_id": effective_session_id,
                        "human_prompt": result.get("human_prompt") or "",
                    }
                )
                return
            # 会话行已在本请求开头 ensure（含匿名首轮）；此处对匿名会话同样
            # 落库 —— 客户端凭 phase/start 公布的 id 即可在 /history 看到本轮。
            if session_store is not None:
                try:
                    session_store.record_turn(
                        effective_session_id, req.task, result.get("final_answer", "")
                    )
                except Exception:
                    logger.exception(
                        "record_turn failed for session {!r}", effective_session_id
                    )
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
                    checkpointer=_active_checkpointer(),
                )
            except Exception as exc:
                logger.exception("chat resume failed for session {!r}", req.session_id)
                yield _sse({"type": "error", "message": f"恢复任务失败：{exc}"})
                yield _sse({"type": "done", "stop_reason": "error", "result_data": {}})
                return
            # H3：resume 成功也落库（task 为原始任务，来自 checkpoint state
            # values）。会话行缺失时（行被删 / 修复前遗留 checkpoint）先重建行
            # 再 record_turn，否则 /history 404 且 /sessions 不显示该会话。
            if session_store is not None:
                task = result.get("task") or ""
                if task and result.get("final_answer"):
                    try:
                        if session_store.get_session(req.session_id) is None:
                            session_store.create_session(
                                req.session_id, session_id=req.session_id
                            )
                        session_store.record_turn(
                            req.session_id, task, result.get("final_answer", "")
                        )
                    except Exception:
                        logger.exception(
                            "record_turn failed for session {!r}", req.session_id
                        )
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
    """Assemble the production wiring from Settings (BP_ env vars / .env).

    checkpointer 不在此同步构造（aiosqlite 连接是异步的）：create_app 的
    lifespan 会按 settings.checkpoint_db_path 创建共享 AsyncSqliteSaver。
    BrowserPool 在此同步构造（无 IO），start()/close() 由 lifespan 负责；
    同一实例既构造注入 executor，又作为 browser_pool 交给生命周期管理。
    池配置全部来自 Settings：headless / max_pages / ``channel``（F3：空串 → None，见
    ``Settings.browser_channel``——``channel=""`` 不是合法值，会直接启动失败）。
    """
    from browsepilot.agent.tracer import JsonlTracer
    from browsepilot.app.browser.browser_pool import BrowserPool
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
    browser_pool = BrowserPool(
        headless=settings.browser_headless,
        max_pages=settings.browser_max_pages,
        channel=settings.browser_channel or None,  # F3：空 = 自带 Chromium
    )
    executor = AppToolExecutor(
        WeatherService(), SearchService(), PageFetchService(), browser=browser_pool
    )
    tracer = JsonlTracer(out_dir=Path(settings.trace_dir))
    session_store = SessionStore(Path(settings.session_db_path))
    return create_app(
        settings,
        provider=provider,
        executor=executor,
        tracer=tracer,
        session_store=session_store,
        rate_limiter=SlidingWindowRateLimiter(
            settings.rate_limit_requests, settings.rate_limit_window_seconds
        ),
        browser_pool=browser_pool,
    )


app = _default_app()
