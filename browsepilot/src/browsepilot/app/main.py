"""V2 FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from browsepilot import __version__
from browsepilot.app.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("browsepilot v2.0 app starting (model={})", settings.default_model)
        yield
        logger.info("browsepilot app shutting down")

    app = FastAPI(title="BrowsePilot API", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__, "model": settings.default_model}

    return app


app = create_app()
