"""ToolExecutor port — the agent layer's only window to the outside world.

MCP servers, local functions, and HTTP endpoints are all implementations
of this protocol. The agent core never imports MCP code.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    status: Literal["success", "error"] = "success"
    content: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    error_type: str = ""
    error_message: str = ""


@runtime_checkable
class ToolExecutor(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...
