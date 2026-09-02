from browsepilot.app.sessions import SessionStore


def test_session_store_crud(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite")
    sid = store.create_session("thread-1")
    info = store.get_session(sid)
    assert info is not None and info.thread_id == "thread-1"
    assert info.pinned is False

    store.rename_session(sid, "天气会话")
    assert store.get_session(sid).name == "天气会话"

    assert store.toggle_pin(sid) is True
    listed = store.list_sessions()
    assert listed[0].id == sid  # pinned first

    store.delete_session(sid)
    assert store.get_session(sid) is None


def test_create_session_with_explicit_id(tmp_path):
    """调用方提供 session_id 时行 id 用给定值（chat/stream 需要行 id ==
    对外公布的 effective session_id）；缺省时仍自生成短 id（向后兼容）。"""
    store = SessionStore(tmp_path / "s.sqlite")
    sid = store.create_session("thread-1", session_id="fixed-session-1")
    assert sid == "fixed-session-1"
    info = store.get_session("fixed-session-1")
    assert info is not None and info.thread_id == "thread-1"
    # 不传 session_id：行为与旧签名一致（自生成 id，行可查）
    auto = store.create_session("thread-2")
    assert store.get_session(auto) is not None


def test_record_and_list_turns(tmp_path):
    store = SessionStore(tmp_path / "s.sqlite")
    sid = store.create_session("t")
    store.record_turn(sid, "查天气", "晴")
    store.record_turn(sid, "查新闻", "ok")
    turns = store.list_turns(sid)
    assert [t["task"] for t in turns] == ["查天气", "查新闻"]


def test_list_sessions_pinned_first_then_recency(tmp_path):
    store = SessionStore(tmp_path / "s.sqlite")
    a = store.create_session("t-a")
    b = store.create_session("t-b")
    store.toggle_pin(b)
    listed = store.list_sessions()
    assert [s.id for s in listed] == [b, a]
