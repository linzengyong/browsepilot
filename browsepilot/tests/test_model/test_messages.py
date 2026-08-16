from browsepilot.model.messages import ChatResult, UsageInfo


def test_usage_info_defaults():
    u = UsageInfo()
    assert u.usage.total_tokens == 0
    assert u.cost.total == 0.0
    assert u.estimated is False


def test_chat_result_defaults():
    r = ChatResult()
    assert r.text == ""
    assert r.raw == {}
