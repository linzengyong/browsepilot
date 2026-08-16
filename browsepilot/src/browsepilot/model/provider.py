"""ModelProvider port — the model layer's only public entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from browsepilot.model.messages import ChatResult, Message


@runtime_checkable
class ModelProvider(Protocol):
    async def chat(self, messages: list[Message], *, model: str | None = None) -> ChatResult: ...

    def stream(
        self, messages: list[Message], *, model: str | None = None
    ) -> AsyncIterator[str]: ...

    async def structured(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel],
        model: str | None = None,
        max_retries: int = 3,
    ) -> BaseModel: ...
