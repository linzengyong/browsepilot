import types
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from browsepilot.model.litellm_provider import (
    LiteLLMProvider,
    StructuredOutputError,
    schema_for,
    tool_spec_for,
)
from browsepilot.model.messages import Message


class Weather(BaseModel):
    """当前天气"""

    city: str
    temperature_c: float


def test_schema_for_adds_description():
    schema = schema_for(Weather)
    assert schema["description"] == "当前天气"
    assert "city" in schema["properties"]


def test_tool_spec_for_builds_function_tool():
    spec = tool_spec_for(schema_for(Weather))
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "emit_result"
    assert "temperature_c" in spec["function"]["parameters"]["properties"]


class _FakeToolMsg:
    tool_calls = [
        types.SimpleNamespace(
            function=types.SimpleNamespace(arguments='{"city": "北京", "temperature_c": 26.5}')
        )
    ]


class _FakeToolChoice:
    message = _FakeToolMsg()


class _FakeToolResp:
    choices = [_FakeToolChoice()]


class _FakeBadToolMsg:
    tool_calls = [
        types.SimpleNamespace(
            function=types.SimpleNamespace(arguments='{"city": 123, "temperature_c": "x"}')
        )
    ]


class _FakeBadToolChoice:
    message = _FakeBadToolMsg()


class _FakeBadToolResp:
    choices = [_FakeBadToolChoice()]


@pytest.mark.asyncio
async def test_structured_returns_validated_model(monkeypatch):
    provider = LiteLLMProvider("deepseek/deepseek-chat")
    monkeypatch.setattr("litellm.acompletion", AsyncMock(return_value=_FakeToolResp()))
    result = await provider.structured([Message(role="user", content="北京天气")], schema=Weather)
    assert isinstance(result, Weather)
    assert result.city == "北京"
    assert result.temperature_c == 26.5


@pytest.mark.asyncio
async def test_structured_retries_then_raises(monkeypatch):
    provider = LiteLLMProvider("deepseek/deepseek-chat")
    monkeypatch.setattr("litellm.acompletion", AsyncMock(return_value=_FakeBadToolResp()))
    with pytest.raises(StructuredOutputError):
        await provider.structured(
            [Message(role="user", content="北京天气")], schema=Weather, max_retries=2
        )
