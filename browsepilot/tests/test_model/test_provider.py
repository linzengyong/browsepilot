import types
from unittest.mock import AsyncMock

import pytest

from browsepilot.model.messages import Message
from browsepilot.model.provider import ModelProvider
from browsepilot.model.litellm_provider import LiteLLMProvider


class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15
    cost = 0.001


class _FakeMsg:
    content = "hello world"


class _FakeChoice:
    message = _FakeMsg()


class _FakeResp:
    choices = [_FakeChoice()]
    usage = _FakeUsage()

    def model_dump(self):
        return {"choices": [{"message": {"content": "hello world"}}]}


def test_litellm_provider_implements_protocol():
    assert isinstance(LiteLLMProvider("deepseek/deepseek-chat"), ModelProvider)


@pytest.mark.asyncio
async def test_chat_returns_text_and_usage(monkeypatch):
    provider = LiteLLMProvider("deepseek/deepseek-chat")
    monkeypatch.setattr("litellm.acompletion", AsyncMock(return_value=_FakeResp()))
    result = await provider.chat([Message(role="user", content="hi")])
    assert result.text == "hello world"
    assert result.usage.usage.input == 10
    assert result.usage.usage.output == 5
    assert result.usage.cost.total == 0.001
    assert result.usage.estimated is False


class _FakeChunk:
    def __init__(self, text: str):
        self.choices = [types.SimpleNamespace(delta=types.SimpleNamespace(content=text))]


@pytest.mark.asyncio
async def test_stream_yields_text_chunks(monkeypatch):
    provider = LiteLLMProvider("deepseek/deepseek-chat")

    async def fake_stream(*args, **kwargs):
        async def gen():
            for t in ["你", "好"]:
                yield _FakeChunk(t)

        return gen()

    monkeypatch.setattr("litellm.acompletion", fake_stream)
    chunks = [c async for c in provider.stream([Message(role="user", content="hi")])]
    assert chunks == ["你", "好"]
