"""V2 FastAPI application factory — the composition root."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from browsepilot import __version__
from browsepilot.agent.api import run_task
from browsepilot.agent.tools import ToolExecutor
from browsepilot.agent.tracer import Tracer
from browsepilot.app.config import Settings
from browsepilot.model.provider import ModelProvider


class ChatRequest(BaseModel):
    task: str = Field(min_length=1)
    session_id: str = ""


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_app(
    settings: Settings | None = None,
    *,
    provider: ModelProvider,
    executor: ToolExecutor,
    tracer: Tracer,
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

    @app.post("/chat/stream")
    async def chat_stream(req: ChatRequest):
        """执行一个用户任务，SSE 返回阶段事件与最终回答。"""
        async def gen():
            yield _sse({"type": "phase", "phase": "start", "task": req.task[:100]})
            result = await run_task(
                req.task,
                provider=provider,
                executor=executor,
                tracer=tracer,
                session_id=req.session_id,
            )
            yield _sse({"type": "answer", "text": result.get("final_answer", "")})
            yield _sse({"type": "done", "stop_reason": result.get("stop_reason", ""),
                        "result_data": result.get("result_data", {})})

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
    return create_app(settings, provider=provider, executor=executor, tracer=tracer)


app = _default_app()
