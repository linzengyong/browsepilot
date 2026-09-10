from browsepilot.agent.params import BrowserParams, ReadPageParams, SearchParams, WeatherParams


def test_params_models():
    w = WeatherParams(city="北京")
    s = SearchParams(query="q")
    p = ReadPageParams(url="https://example.com")
    assert w.city == "北京" and s.query == "q" and p.url == "https://example.com"


def test_browser_params_defaults_are_empty():
    """浏览器参数全可选：不同工具从同一模型里各取所需（snapshot 一个都不用）。"""
    params = BrowserParams()
    assert params.url == ""
    assert params.target == ""
    assert params.text == ""
    assert params.direction == "down"
    assert params.expression == ""


def test_browser_params_carry_values():
    params = BrowserParams(
        url="https://example.com/",
        target="e2",
        text="蓝牙耳机",
        direction="up",
        expression="document.title",
    )
    assert params.model_dump() == {
        "url": "https://example.com/",
        "target": "e2",
        "text": "蓝牙耳机",
        "direction": "up",
        "expression": "document.title",
    }
