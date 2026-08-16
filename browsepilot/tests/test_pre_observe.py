"""Tests for pre_observe_node."""

import os
import re
import sys
from unittest.mock import AsyncMock

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.agent.nodes import pre_observe_node


def test_pre_observe_callable():
    assert callable(pre_observe_node)


def test_url_extraction_from_task():
    url_pat = r"https?://\S+"
    assert re.search(url_pat, "打开 https://www.bing.com 搜索")
    assert not re.search(url_pat, "搜索机械键盘")


def test_url_strip_trailing_punctuation():
    """URL regex should not capture trailing Chinese punctuation."""
    m = re.search(r"https?://\S+", "打开 https://github.com，")
    raw = m.group(0)
    clean = re.sub(r"[，。！？；：\"\"''（）【】、《》…—]+$", "", raw)
    assert clean == "https://github.com"


def test_default_search_url():
    from backend.app.config import settings

    assert settings.default_search_url
    assert "." in settings.default_search_url


def test_state_fields_exist():
    from backend.app.agent.state import AgentState

    assert "page_structure" in AgentState.__annotations__
    assert "page_screenshot" in AgentState.__annotations__


@pytest.mark.asyncio
async def test_pre_observe_url_extraction():
    """With a real URL in task, navigate to that URL (mock MCP)."""
    mock_mcp = AsyncMock()
    mock_mcp.call_tool.return_value = {
        "status": "success",
        "structure": {"inputs": [], "buttons": [], "links": []},
    }
    state = {"task": "打开 https://github.com 搜索项目"}

    from backend.app.config import settings

    settings.llm_vision_enabled = False

    await pre_observe_node(state, mock_mcp)

    # Should have called navigate with the URL from task
    navigate_call = mock_mcp.call_tool.call_args_list[0]
    assert navigate_call[0][0] in ("navigate", "browser_navigate")
    assert "github.com" in navigate_call[0][1]["url"]


@pytest.mark.asyncio
async def test_pre_observe_no_url_uses_default():
    """Without URL in task, navigate to default search URL."""
    mock_mcp = AsyncMock()
    mock_mcp.call_tool.return_value = {"status": "success", "structure": {}}
    state = {"task": "搜索机械键盘"}

    from backend.app.config import settings

    settings.llm_vision_enabled = False

    await pre_observe_node(state, mock_mcp)

    navigate_call = mock_mcp.call_tool.call_args_list[0]
    assert "bing.com" in navigate_call[0][1]["url"]


@pytest.mark.asyncio
async def test_pre_observe_navigate_failure():
    """navigate 失败时 page_structure 仍尝试获取."""
    mock_mcp = AsyncMock()
    mock_mcp.call_tool.side_effect = [
        Exception("navigate failed"),  # navigate raises
        Exception("get_page_structure failed"),  # get_page_structure raises too
    ]
    state = {"task": "搜索测试"}

    from backend.app.config import settings

    settings.llm_vision_enabled = False

    result = await pre_observe_node(state, mock_mcp)
    assert result["page_structure"] == {}
    assert result["page_screenshot"] == ""


@pytest.mark.asyncio
async def test_pre_observe_vision_disabled_no_screenshot():
    """Screenshot always captured for UI replay even with vision disabled."""
    mock_mcp = AsyncMock()
    mock_mcp.call_tool.return_value = {"status": "success", "structure": {}}
    state = {"task": "搜索测试"}

    from backend.app.config import settings

    settings.llm_vision_enabled = False

    await pre_observe_node(state, mock_mcp)
    # Screenshot is always captured now
    assert mock_mcp.call_tool.call_count == 3
