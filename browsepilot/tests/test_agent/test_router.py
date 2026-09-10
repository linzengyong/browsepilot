from browsepilot.agent.router import classify_by_rules


def test_weather_rule():
    d = classify_by_rules("北京今天天气怎么样？")
    assert d is not None
    assert d.intent == "weather" and d.tier == "tier1" and d.confidence == 1.0


def test_search_rule():
    d = classify_by_rules("搜索一下 Python 3.13 正式发布时间")
    assert d is not None
    assert d.intent == "search" and d.tier == "tier1"


def test_read_page_rule_by_url():
    d = classify_by_rules("打开 https://www.python.org 告诉我页面主要内容")
    assert d is not None
    assert d.intent == "read_page" and d.tier == "tier2"


def test_chitchat_rule():
    d = classify_by_rules("你好")
    assert d is not None
    assert d.intent == "chitchat" and d.tier == "answer"


def test_knowledge_qa_rule():
    d = classify_by_rules("什么是 HTTP/2？")
    assert d is not None
    assert d.intent == "knowledge_qa" and d.tier == "answer"


def test_unknown_task_returns_none():
    assert classify_by_rules("帮我处理一下那个事情") is None


def test_browser_task_interaction_rule():
    d = classify_by_rules("帮我点击登录按钮并填写手机号")
    assert d is not None
    assert d.intent == "browser_task" and d.tier == "tier3"


def test_browser_task_commerce_rule():
    d = classify_by_rules("帮我全网比价 iPhone 和 小米")
    assert d is not None
    assert d.intent == "browser_task" and d.tier == "tier3"


def test_browser_task_site_rule_beats_generic_search():
    """带站点/交互标记的任务是 tier3（具体规则优先于泛化的 search 规则）。"""
    d = classify_by_rules("在京东上搜索蓝牙耳机")
    assert d is not None
    assert d.intent == "browser_task" and d.tier == "tier3"


def test_browser_task_does_not_steal_search_rule():
    """纯搜索任务仍走 tier1 search（browser_task 不抢泛判）。"""
    d = classify_by_rules("帮我找一下 Python 异步编程教程")
    assert d is not None
    assert d.intent == "search" and d.tier == "tier1"


def test_browser_task_does_not_steal_knowledge_qa():
    """教科书式提问（什么是/解释/原理）优先于其中的交互词。"""
    d = classify_by_rules("解释什么是表单登录的 CSRF 原理")
    assert d is not None
    assert d.intent == "knowledge_qa" and d.tier == "answer"


def test_browser_task_does_not_steal_read_page_or_weather():
    """打开/访问 仍是 tier2；天气仍是 tier1（browser_task 排在它们之后）。"""
    assert classify_by_rules("打开百度首页").intent == "read_page"
    assert classify_by_rules("打开百度首页").tier == "tier2"
    d = classify_by_rules("京东那边今天天气怎么样")
    assert d.intent == "weather" and d.tier == "tier1"
