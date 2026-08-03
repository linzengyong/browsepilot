from browsepilot.model.messages import ChatResult, Message, UsageInfo


def test_message_to_openai_user():
    m = Message(role="user", content="你好")
    assert m.to_openai() == {"role": "user", "content": "你好"}


def test_message_to_openai_tool_role():
    m = Message(role="tool", content="ok", tool_call_id="call_1")
    out = m.to_openai()
    assert out["tool_call_id"] == "call_1"


def test_usage_info_defaults():
    u = UsageInfo()
    assert u.usage.total_tokens == 0
    assert u.cost.total == 0.0
    assert u.estimated is False


def test_chat_result_defaults():
    r = ChatResult()
    assert r.text == ""
    assert r.raw == {}
