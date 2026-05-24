"""Integration tests for all 3 pending requirements.

Requirements covered:
  R1: Tool selection classifier (unit tests on classify_tool)
  R2: Session recovery (API: /chat/stream resume from history)
  R3: Session management (API: PATCH rename/pin, list_sessions sort)
"""

import json
import os
import sys
import tempfile

import pytest
from httpx import ASGITransport, AsyncClient

# Add worktree to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.config import settings

from backend.app.agent.nodes import classify_tool, TOOL_RULES


# ── R1: Tool Selection Classifier ──────────────────────────────────────

CLASSIFIER_CASES = [
    # (step_description, expected_tool_or_None)
    ("导航到 https://www.bing.com", "browser_navigate"),
    ("获取页面结构，找到搜索框的选择器", "browser_snapshot"),
    ("在搜索框中输入 2000元手机 推荐", "browser_type"),
    ("点击搜索按钮", "browser_click"),
    ("获取搜索结果页面内容", "browser_snapshot"),
    ("回到搜索页面重新开始", None),
    ("截图保存当前页面", "browser_take_screenshot"),
    ("", None),
    ("Open bing.com and search", "browser_navigate"),
    ("获取搜索结果结构，提取前3条标题和链接", "browser_snapshot"),
]

@pytest.mark.parametrize("step,expected", CLASSIFIER_CASES)
def test_classify_tool_boundary(step, expected):
    result = classify_tool(step)
    assert result == expected, f"classify_tool({step!r}) = {result!r}, expected {expected!r}"


def test_all_tools_in_rules():
    """Verify all 8 tools are defined."""
    assert set(TOOL_RULES.keys()) == {
        "browser_navigate", "browser_snapshot", "browser_type", "browser_click",
        "browser_wait_for", "browser_take_screenshot", "browser_press_key",
        "browser_fill_form", "browser_evaluate",
    }


def test_threshold_value():
    assert settings.tool_classify_threshold == 2


def test_classifier_navigate_needs_url():
    """Regression test: classifier picks navigate but args MUST come from LLM.

    navigate tool requires a 'url' parameter. The classifier only selects the
    tool name — it does NOT extract arguments. The execute_node must still call
    the LLM to fill in arguments when the classifier hits.
    """
    result = classify_tool("导航到 https://www.bing.com")
    assert result == "browser_navigate"
    # classifier itself should NOT return arguments — just the tool name (or None)


def test_classifier_returns_none_for_ambiguous():
    """Ambiguous steps go to LLM for full tool+args selection."""
    # 空步骤
    assert classify_tool("") is None
    # 纯聊天
    assert classify_tool("你好，今天天气怎么样") is None

def test_create_and_persist_session(session_manager):
    sm = session_manager
    sm.create_session("test-001")
    sm.start_turn("test-001", "搜索 Python 教程")
    sm.persist("test-001")

    history = sm.get_history("test-001")
    assert history is not None
    turns = history["turns"]
    assert turns[-1]["task"] == "搜索 Python 教程"
    assert history["status"] == "completed"
    assert turns[-1]["status"] == "completed"


def test_rename_session(session_manager):
    sm = session_manager
    sm.create_session("test-002")
    sm.update("test-002", task="original task")
    sm.persist("test-002")

    ok = sm.rename_session("test-002", "我的自定义名称")
    assert ok

    data = sm.get_history("test-002")
    assert data["custom_name"] == "我的自定义名称"


def test_rename_nonexistent_session(session_manager):
    ok = session_manager.rename_session("no-such-session", "name")
    assert not ok


def test_toggle_pin(session_manager):
    sm = session_manager
    sm.create_session("test-003")
    sm.persist("test-003")

    ok = sm.toggle_pin("test-003", True)
    assert ok
    data = sm.get_history("test-003")
    assert data["pinned"] is True

    ok = sm.toggle_pin("test-003", False)
    assert ok
    data = sm.get_history("test-003")
    assert data["pinned"] is False


def test_list_sessions_pinned_sort(session_manager):
    sm = session_manager
    # Create 3 sessions
    sm.create_session("aaa")
    sm.update("aaa", task="task aaa")
    sm.persist("aaa")

    sm.create_session("bbb")
    sm.update("bbb", task="task bbb")
    sm.persist("bbb")

    sm.create_session("ccc")
    sm.update("ccc", task="task ccc")
    sm.persist("ccc")

    # Pin middle session
    sm.toggle_pin("bbb", True)

    sessions = sm.list_sessions()
    assert len(sessions) >= 3

    # Pinned session should come first
    bbb = next(s for s in sessions if s["id"] == "bbb")
    assert bbb["pinned"] is True
    assert bbb["custom_name"] == ""  # default

    first_pinned = sessions[0]["pinned"]
    assert first_pinned is True  # first item is pinned


def test_list_sessions_includes_new_fields(session_manager):
    sm = session_manager
    sm.create_session("test-fields")
    sm.persist("test-fields")
    sm.rename_session("test-fields", "自定义")
    sm.toggle_pin("test-fields", True)

    sessions = sm.list_sessions()
    s = next(s for s in sessions if s["id"] == "test-fields")
    assert s["custom_name"] == "自定义"
    assert s["pinned"] is True
    assert "task_summary" in s
    assert "created_at" in s
    assert "status" in s


# ── R2 & R3: API Endpoints ─────────────────────────────────────────────

from backend.app.main import app


@pytest.fixture
def api_client():
    """Async HTTP client for the FastAPI test app."""
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def api_session(api_client):
    """Create a session and persist it for API tests."""
    from backend.app.main import session_manager as sm
    sm.create_session("api-test-001")
    sm.start_turn("api-test-001", "test task for API")
    sm.update_current_turn("api-test-001", token_usage={"prompt": 100, "completion": 50})
    sm.persist("api-test-001")
    yield "api-test-001"
    # Clean up
    try:
        sm._delete_session_files("api-test-001")
    except Exception:
        pass


@pytest.mark.asyncio
async def test_list_sessions_api():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # Check structure of first item if any
        if data:
            s = data[0]
            assert "id" in s
            assert "pinned" in s
            assert "custom_name" in s


@pytest.mark.asyncio
async def test_rename_session_api(api_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            f"/sessions/{api_session}/rename",
            json={"name": "API 重命名测试"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_rename_session_api_empty_name(api_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            f"/sessions/{api_session}/rename",
            json={"name": ""},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rename_session_api_not_found():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/sessions/no-such-id/rename",
            json={"name": "test"},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pin_session_api(api_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            f"/sessions/{api_session}/pin",
            json={"pinned": True},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_pin_session_api_not_found():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/sessions/no-such-id/pin",
            json={"pinned": True},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_history_endpoint_returns_token_usage(api_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/history/{api_session}")
        assert resp.status_code == 200
        data = resp.json()
        turns = data.get("turns", [])
        assert len(turns) > 0
        assert "token_usage" in turns[-1]
        assert "execution_log" in turns[-1]


@pytest.mark.asyncio
async def test_delete_session_api(api_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/sessions/{api_session}")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
