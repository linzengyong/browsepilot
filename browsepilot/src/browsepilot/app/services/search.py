"""Tier1 search service backed by ddgs (DuckDuckGo, no API key)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class SearchError(RuntimeError):
    pass


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""


def _ddgs_text(query: str, max_results: int = 5) -> list[dict]:
    from ddgs import DDGS

    with DDGS() as d:
        return list(d.text(query, max_results=max_results))


class SearchService:
    def __init__(self, search_fn: Callable[..., list[dict]] | None = None):
        self._search_fn = search_fn or _ddgs_text

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        @retry(
            stop=stop_after_attempt(2),
            retry=retry_if_exception_type((Exception,)),
            wait=wait_exponential(multiplier=1, min=1, max=5),
            reraise=True,
        )
        async def _run() -> list[SearchResult]:
            # ddgs is sync; run in a thread to avoid blocking the event loop
            raw = await asyncio.to_thread(self._search_fn, query, max_results)
            if not raw:
                raise SearchError(f"no results for query: {query!r}")
            return [
                SearchResult(
                    title=r.get("title", ""),
                    url=r.get("href", ""),
                    snippet=r.get("body", "") or "",
                )
                for r in raw
            ]

        try:
            return await _run()
        except SearchError:
            raise
        except Exception as exc:
            raise SearchError(f"search failed for {query!r}: {exc}") from exc
