"""Provider-agnostic helpers for structured LLM output (article 2 material).

Strategy: forced function calling (tool_choice) + Pydantic validation + retry.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

TOOL_NAME = "emit_result"


class StructuredOutputError(RuntimeError):
    pass


def schema_for(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.setdefault("description", model.__doc__ or model.__name__)
    return schema


def tool_spec_for(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": schema.get("description", ""),
            "parameters": schema,
        },
    }
