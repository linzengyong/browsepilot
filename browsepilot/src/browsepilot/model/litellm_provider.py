"""LiteLLM-backed ModelProvider implementation (multi-provider).

`model` is a LiteLLM model string, e.g.
'deepseek/deepseek-chat', 'anthropic/claude-3-7-sonnet-20250219', 'ollama/llama3'.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from loguru import logger
from pydantic import BaseModel, ValidationError
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from browsepilot.model.messages import ChatResult, Cost, Message, Usage, UsageInfo
from browsepilot.model.structured import (
    StructuredOutputError,
    schema_for,
    tool_spec_for,
)


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Serialize unified messages to LiteLLM's OpenAI-style wire format.

    Kept in the adapter (not on Message) so the unified model stays
    provider-neutral: the wire format is a LiteLLM contract, not part of
    the message's meaning.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_call_id is not None:
            item["tool_call_id"] = m.tool_call_id
        if m.name is not None:
            item["name"] = m.name
        out.append(item)
    return out


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

    async def chat(self, messages: list[Message], *, model: str | None = None) -> ChatResult:
        import litellm

        resp = await litellm.acompletion(
            model=model or self.model,
            messages=to_openai_messages(messages),
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
            messages=to_openai_messages(messages),
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
        import litellm

        schema_dict = schema_for(schema)
        tool = tool_spec_for(schema_dict)
        msgs = to_openai_messages(messages)

        @retry(
            stop=stop_after_attempt(max_retries),
            retry=retry_if_exception_type(
                (IndexError, AttributeError, json.JSONDecodeError, ValidationError)
            ),
            wait=wait_fixed(0),  # LLM 重试保持零等待（与 P0 行为一致）；后续可换 wait_exponential
        )
        async def _attempt() -> BaseModel:
            resp = await litellm.acompletion(
                model=model or self.model,
                messages=msgs,
                tools=[tool],
                tool_choice={
                    "type": "function",
                    "function": {"name": tool["function"]["name"]},
                },
                **self._kwargs(),
            )
            call = resp.choices[0].message.tool_calls[0].function
            data = json.loads(call.arguments)
            return schema(**data)

        try:
            return await _attempt()
        except RetryError as exc:
            last = exc.last_attempt.exception()
            logger.warning("structured output failed after {} attempts: {}", max_retries, last)
            raise StructuredOutputError(
                f"structured output failed after {max_retries} attempts: {last}"
            ) from last
