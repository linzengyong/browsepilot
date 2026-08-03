"""LiteLLM-backed ModelProvider implementation (multi-provider).

`model` is a LiteLLM model string, e.g.
'deepseek/deepseek-chat', 'anthropic/claude-3-7-sonnet-20250219', 'ollama/llama3'.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from browsepilot.model.messages import ChatResult, Cost, Message, Usage, UsageInfo
from browsepilot.model.provider import ModelProvider


class LiteLLMProvider:
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    def _kwargs(self) -> dict:
        kw: dict = {}
        if self.api_key:
            kw["api_key"] = self.api_key
        if self.base_url:
            kw["base_url"] = self.base_url
        return kw

    @staticmethod
    def _to_usage(usage: Any) -> UsageInfo:
        if usage is None:
            return UsageInfo(estimated=True)
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cache_read = 0
        if prompt_details is not None:
            cache_read = getattr(prompt_details, "cached_tokens", 0) or 0
        return UsageInfo(
            usage=Usage(
                input=getattr(usage, "prompt_tokens", 0),
                output=getattr(usage, "completion_tokens", 0),
                cache_read=cache_read,
                total_tokens=getattr(usage, "total_tokens", 0),
            ),
            cost=Cost(total=getattr(usage, "cost", 0.0) or 0.0),
            estimated=False,
        )

    async def chat(
        self, messages: list[Message], *, model: str | None = None
    ) -> ChatResult:
        import litellm

        resp = await litellm.acompletion(
            model=model or self.model,
            messages=[m.to_openai() for m in messages],
            **self._kwargs(),
        )
        text = resp.choices[0].message.content or ""
        return ChatResult(
            text=text,
            usage=self._to_usage(resp.usage),
            raw=resp.model_dump(),
        )

    async def stream(
        self, messages: list[Message], *, model: str | None = None
    ) -> AsyncIterator[str]:
        import litellm

        stream = await litellm.acompletion(
            model=model or self.model,
            messages=[m.to_openai() for m in messages],
            stream=True,
            **self._kwargs(),
        )
        async for chunk in stream:
            delta = getattr(chunk.choices[0], "delta", None)
            if delta is None:
                continue
            text = getattr(delta, "content", None)
            if text:
                yield text

    async def structured(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel],
        model: str | None = None,
        max_retries: int = 3,
    ) -> BaseModel:
        """Structured output — declared for the ModelProvider protocol.

        Full implementation lands in T5 (forced tool calling + Pydantic
        validation + retry, see model/structured.py).
        """
        raise NotImplementedError("structured() is implemented in T5")
