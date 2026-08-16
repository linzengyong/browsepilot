import httpx
import pytest

from browsepilot.app.services.fetch import FetchError, PageFetchService

SAMPLE_HTML = """<!DOCTYPE html><html><head><title>测试页</title></head>
<body><article><h1>标题</h1>
<p>这是正文第一段，包含有用的信息。人工智能和机器学习正在改变我们的生活方式，从智能助手到自动驾驶汽车，技术革新无处不在。</p>
<p>这是正文第二段，信息量充足。随着大语言模型的进步，自然语言处理能力大幅提升，人们可以更自然地与计算机交互，完成复杂的任务。</p>
<p>这是正文第三段。本段继续补充背景信息，讨论技术落地过程中遇到的挑战与机遇，以及未来可能的发展方向和应用场景。</p></article>
<nav>导航噪音</nav><footer>页脚噪音</footer></body></html>"""  # noqa: E501  (3-paragraph article: the original ~40-char fixture fell below trafilatura 2.2.0's per-article gate baseline._MIN_CONTENT_LENGTH=100, triggering the baseline rescue that keeps <nav>; see task-5 report)


@pytest.mark.asyncio
async def test_fetch_extracts_main_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=SAMPLE_HTML, headers={"content-type": "text/html; charset=utf-8"}
        )

    service = PageFetchService(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    page = await service.fetch("https://example.com/article")
    assert page.url == "https://example.com/article"
    assert "正文第一段" in page.text
    assert "导航噪音" not in page.text  # trafilatura removes boilerplate


@pytest.mark.asyncio
async def test_fetch_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    service = PageFetchService(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(FetchError):
        await service.fetch("https://example.com/broken")


@pytest.mark.asyncio
async def test_fetch_truncates_long_text():
    def handler(request: httpx.Request) -> httpx.Response:
        body = "<html><body><p>" + "长" * 20000 + "</p></body></html>"
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    service = PageFetchService(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    page = await service.fetch("https://example.com/long")
    assert len(page.text) <= 8000
