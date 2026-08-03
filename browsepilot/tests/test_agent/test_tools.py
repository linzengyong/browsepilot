import pytest
from pydantic import ValidationError

from browsepilot.agent.tools import ToolExecutor, ToolResult, ToolSpec


class FakeExecutor:
    async def list_tools(self):
        return [ToolSpec(name="echo")]

    async def call(self, name, arguments):
        return ToolResult(content="ok")


def test_tool_executor_protocol():
    assert isinstance(FakeExecutor(), ToolExecutor)


def test_tool_spec_defaults():
    s = ToolSpec(name="a")
    assert s.description == ""
    assert s.parameters == {}


def test_tool_result_error_shape():
    r = ToolResult(status="error", error_type="timeout", error_message="x")
    assert r.status == "error"
    assert r.error_type == "timeout"


def test_tool_result_status_must_be_valid():
    with pytest.raises(ValidationError):
        ToolResult(status="bogus")
