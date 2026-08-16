from fastapi.testclient import TestClient

from browsepilot.agent.tools import ToolResult, ToolSpec
from browsepilot.agent.tracer import JsonlTracer
from browsepilot.app.config import Settings
from browsepilot.app.main import create_app
from browsepilot.model.messages import ChatResult


class FakeProvider:
    async def chat(self, messages, *, model=None):
        return ChatResult(text="合成回答", raw={})

    async def stream(self, messages, *, model=None):
        yield "合"
        yield "成"

    async def structured(self, messages, *, schema, model=None, max_retries=3):
        # 按 schema 字段给参数：city → 北京；query → q；url → example.com
        if "city" in schema.model_fields:
            return schema(city="北京")
        if "query" in schema.model_fields:
            return schema(query="q")
        if "url" in schema.model_fields:
            return schema(url="https://example.com")
        return schema()


class FakeExecutor:
    async def list_tools(self):
        return [ToolSpec(name="weather")]

    async def call(self, name, arguments):
        return ToolResult(status="success", data={"city": "北京", "temperature_c": 25.0})


def _make_app(tmp_path, settings=None):
    return create_app(
        settings,
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=JsonlTracer(trace_id="api-test", out_dir=tmp_path),
    )


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.default_model == "deepseek/deepseek-chat"
    assert s.navigation_policy == "allow_https"
    assert s.allowed_domains == []
    assert s.max_steps == 30


def test_health_endpoint(tmp_path):
    client = TestClient(_make_app(tmp_path))
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == "2.0.0"


def test_health_reflects_settings(tmp_path):
    client = TestClient(
        _make_app(tmp_path, settings=Settings(_env_file=None, default_model="test/model"))
    )
    assert client.get("/health").json()["model"] == "test/model"


def test_chat_stream_endpoint(tmp_path):
    app = create_app(
        Settings(_env_file=None),
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=JsonlTracer(trace_id="api-test", out_dir=tmp_path),
    )
    client = TestClient(app)
    with client.stream("POST", "/chat/stream", json={"task": "北京今天天气怎么样？"}) as resp:
        assert resp.status_code == 200
        events = [line for line in resp.iter_lines() if line.startswith("data: ")]
        joined = "\n".join(events)
        assert '"type": "answer"' in joined
        assert '"type": "done"' in joined


def test_chat_stream_rejects_empty_task(tmp_path):
    app = create_app(Settings(_env_file=None), provider=FakeProvider(), executor=FakeExecutor(),
                      tracer=JsonlTracer(trace_id="t", out_dir=tmp_path))
    client = TestClient(app)
    resp = client.post("/chat/stream", json={"task": ""})
    assert resp.status_code == 422
