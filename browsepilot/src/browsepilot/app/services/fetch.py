"""Tier2 page fetch: HTTP GET + readability-style main-text extraction.

No clicking, no interaction — serves "read a page" tasks. trafilatura does
the boilerplate stripping (article 2/3 material: leverage the ecosystem).
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

MAX_TEXT_CHARS = 8000  # keep answers compact; full pages bloat LLM context


class FetchError(RuntimeError):
    pass


class PageContent(BaseModel):
    url: str
    title: str = ""
    text: str


class PageFetchService:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    async def fetch(self, url: str) -> PageContent:
        import trafilatura

        client = self._client or httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 browsepilot/2.0"}, follow_redirects=True
        )
        try:
            resp = await self._get(client, url)
            html = resp.text
            extracted = trafilatura.extract(
                html, include_comments=False, include_links=False, output_format="txt"
            )
            if not extracted:
                raise FetchError(f"no readable content extracted from {url}")
            title = trafilatura.extract_metadata(html).title or ""
            return PageContent(url=url, title=title, text=extracted[:MAX_TEXT_CHARS])
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"fetch failed for {url}: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()

    @retry(
        stop=stop_after_attempt(2),
        retry=retry_if_exception_type((httpx.HTTPError,)),
        wait=wait_exponential(multiplier=1, min=1, max=5),
    )
    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        resp = await client.get(url, timeout=15)
        resp.raise_for_status()
        return resp
