import pytest

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


# ── I1 回归：泛化弱动词不得抢走 search/None 的既有匹配 ──────────────────────────
# 评审探针：下面这些日常提问曾因“选择/提交/输入”这些单字动词被抢成 browser_task，
# 而 tier3 链（snapshot → open）对它们没有 search 兜底 → all_strategies_failed。
# 期望值与加 tier3 规则之前一致（git show 9567a90:browsepilot/src/browsepilot/agent/router.py）。


@pytest.mark.parametrize(
    "task",
    [
        "为什么选择 Python 而不是 Java",
        "查一下怎么提交表单",
    ],
)
def test_weak_interaction_words_do_not_steal_search(task):
    """有 search 关键词的提问仍走 tier1 search（弱动词不抢既有匹配）。"""
    d = classify_by_rules(task)
    assert d is not None
    assert d.intent == "search" and d.tier == "tier1"


@pytest.mark.parametrize(
    "task",
    [
        "选择哪个框架比较好",
        "Python 输入输出教程",
        "git 提交规范",
    ],
)
def test_weak_interaction_words_alone_stay_out_of_tier3(task):
    """弱动词没有页面上下文（按钮/表单/站点…）就不算 tier3：规则层不命中，交给模型兜底。

    “选择哪个框架比较好”还钉住一个细节：裸“框”是页面标记，但“框架”不是。
    """
    assert classify_by_rules(task) is None


def test_weak_interaction_words_with_page_context_are_still_tier3():
    """共现标记保住真正的交互任务：弱动词 + 页面元素/站点词 = tier3。"""
    d = classify_by_rules("帮我填写表单并提交")
    assert d is not None
    assert d.intent == "browser_task" and d.tier == "tier3"
    d = classify_by_rules("在页面上输入关键词")
    assert d is not None
    assert d.intent == "browser_task" and d.tier == "tier3"


def test_strong_site_words_need_no_context():
    """强触发词（站点/电商/搜索框/浏览器）单独出现即可判 tier3（弱动词才要共现）。"""
    d = classify_by_rules("用浏览器帮我看看")
    assert d is not None
    assert d.intent == "browser_task" and d.tier == "tier3"
