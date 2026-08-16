import pytest

from browsepilot.app.services.search import SearchError, SearchResult, SearchService


@pytest.mark.asyncio
async def test_search_returns_structured_results():
    def fake_ddgs(query, max_results=5, **kwargs):
        return [
            {"title": "Python 3.13 发布", "href": "https://example.com/1", "body": "摘要一"},
            {"title": "Python 下载", "href": "https://example.com/2", "body": "摘要二"},
        ]

    service = SearchService(search_fn=fake_ddgs)
    results = await service.search("Python 3.13 发布")
    assert len(results) == 2
    assert results[0].title == "Python 3.13 发布"
    assert results[0].url == "https://example.com/1"
    assert results[0].snippet == "摘要一"


@pytest.mark.asyncio
async def test_search_empty_results_raise():
    def fake_ddgs(query, max_results=5, **kwargs):
        return []

    service = SearchService(search_fn=fake_ddgs)
    with pytest.raises(SearchError):
        await service.search("不存在的关键词xyzabc123")


@pytest.mark.asyncio
async def test_search_fn_failure_raises_after_retries():
    calls = []

    def fake_ddgs(query, max_results=5, **kwargs):
        calls.append(1)
        raise RuntimeError("network down")

    service = SearchService(search_fn=fake_ddgs)
    with pytest.raises(SearchError):
        await service.search("test")
    assert len(calls) == 2  # tenacity: 2 attempts


def test_search_result_defaults():
    r = SearchResult(title="t", url="u")
    assert r.snippet == ""
