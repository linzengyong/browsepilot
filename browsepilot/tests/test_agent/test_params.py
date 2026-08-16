from browsepilot.agent.params import ReadPageParams, SearchParams, WeatherParams


def test_params_models():
    w = WeatherParams(city="北京")
    s = SearchParams(query="q")
    p = ReadPageParams(url="https://example.com")
    assert w.city == "北京" and s.query == "q" and p.url == "https://example.com"
