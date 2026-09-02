import json

from fastapi.testclient import TestClient

from browsepilot.agent.tools import ToolResult, ToolSpec
from browsepilot.agent.tracer import JsonlTracer
from browsepilot.app.config import Settings
from browsepilot.app.main import create_app
from browsepilot.app.sessions import SessionStore
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


class RecordingProvider(FakeProvider):
    """FakeProvider that records the user content of every chat() call."""

    def __init__(self):
        self.prompts: list[str] = []

    async def chat(self, messages, *, model=None):
        user = next((m for m in reversed(messages) if m.role == "user"), None)
        self.prompts.append(user.content if user else "")
        return await super().chat(messages, model=model)


class RaisingProvider:
    async def chat(self, messages, *, model=None):
        raise RuntimeError("boom")


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


def _make_store_app(tmp_path, provider, session_store):
    """App wired with a real SessionStore (multi-turn /history paths)."""
    return create_app(
        Settings(_env_file=None),
        provider=provider,
        executor=FakeExecutor(),
        tracer=JsonlTracer(trace_id="api-test", out_dir=tmp_path),
        session_store=session_store,
    )


def _stream_chat_events(client, session_id: str, task: str) -> list[dict]:
    """POST /chat/stream and consume the whole SSE body.

    记录与错误处理都发生在流式生成器内部，因此必须完整迭代响应体
    （在 stream 上下文内）断言才成立。
    """
    with client.stream(
        "POST", "/chat/stream", json={"task": task, "session_id": session_id}
    ) as resp:
        assert resp.status_code == 200
        return [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]


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
        events = [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]
        assert [e["type"] for e in events] == ["phase", "answer", "done"]
        phase, answer, done = events
        assert phase["phase"] == "start"
        assert phase["task"] == "北京今天天气怎么样？"
        assert answer["text"] == "合成回答"
        assert done["stop_reason"] == "completed"


def test_chat_stream_emits_error_and_done_on_run_task_exception(tmp_path):
    app = create_app(
        Settings(_env_file=None),
        provider=RaisingProvider(),
        executor=FakeExecutor(),
        tracer=JsonlTracer(trace_id="api-test", out_dir=tmp_path),
    )
    client = TestClient(app)
    with client.stream("POST", "/chat/stream", json={"task": "你好"}) as resp:
        assert resp.status_code == 200
        events = [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]
        types = [e["type"] for e in events]
        assert types[0] == "phase"
        error_idx = types.index("error")
        done_idx = types.index("done")
        assert error_idx < done_idx
        assert done_idx == len(events) - 1  # done is terminal
        assert "boom" in events[error_idx]["message"]
        assert events[done_idx]["stop_reason"] == "error"
        assert events[done_idx]["result_data"] == {}
        assert "answer" not in types


def test_chat_stream_rejects_empty_task(tmp_path):
    app = create_app(
        Settings(_env_file=None),
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=JsonlTracer(trace_id="t", out_dir=tmp_path),
    )
    client = TestClient(app)
    resp = client.post("/chat/stream", json={"task": ""})
    assert resp.status_code == 422


def test_multiturn_chat_records_and_feeds_history(tmp_path):
    """HTTP 层多轮闭环：record-on-success → 历史注入 → /history 往返。"""
    store = SessionStore(tmp_path / "s.sqlite")
    sid = store.create_session("thread-x")
    provider = RecordingProvider()
    client = TestClient(_make_store_app(tmp_path, provider, store))

    first = _stream_chat_events(client, sid, "北京天气怎么样？")
    assert [e["type"] for e in first] == ["phase", "answer", "done"]
    assert first[-1]["stop_reason"] == "completed"
    assert "（无历史）" in provider.prompts[0]  # 首轮没有可注入的历史

    second = _stream_chat_events(client, sid, "那上海呢？天气怎么样？")
    assert [e["type"] for e in second] == ["phase", "answer", "done"]
    assert second[-1]["stop_reason"] == "completed"

    # 第二轮模型 prompt 含第一轮的 "Q: … / A: …" 摘要（多轮上下文）
    assert len(provider.prompts) == 2
    assert "北京天气" in provider.prompts[1]
    assert "Q: 北京天气怎么样？ / A: 合成回答" in provider.prompts[1]

    # /history 往返：两轮按顺序落库
    resp = client.get(f"/history/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["id"] == sid
    turns = body["turns"]
    assert [t["task"] for t in turns] == ["北京天气怎么样？", "那上海呢？天气怎么样？"]
    assert [t["answer"] for t in turns] == ["合成回答", "合成回答"]
    assert all(t["created_at"] for t in turns)


def test_chat_error_does_not_record_turn(tmp_path):
    """run_task 抛异常时只发 error/done SSE，且不往会话历史落库。"""
    store = SessionStore(tmp_path / "s.sqlite")
    sid = store.create_session("thread-x")
    client = TestClient(_make_store_app(tmp_path, RaisingProvider(), store))

    events = _stream_chat_events(client, sid, "你好")
    types = [e["type"] for e in events]
    assert "error" in types
    assert types[-1] == "done"
    assert events[-1]["stop_reason"] == "error"
    assert "answer" not in types

    resp = client.get(f"/history/{sid}")
    assert resp.status_code == 200
    assert resp.json()["turns"] == []


def test_history_404_for_missing_session(tmp_path):
    """不存在的会话 id → 404（store 已配置）。"""
    store = SessionStore(tmp_path / "s.sqlite")
    store.create_session("thread-x")
    client = TestClient(_make_store_app(tmp_path, FakeProvider(), store))

    resp = client.get("/history/no-such-session")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "session not found"


def test_history_feed_only_last_five_turns(tmp_path):
    """历史注入窗口截断：>5 轮时只喂最近 5 条，单条超 200 字符截断。"""
    store = SessionStore(tmp_path / "s.sqlite")
    sid = store.create_session("thread-x")
    for i in range(1, 7):
        answer = "长" * 300 if i == 6 else "收到"
        store.record_turn(sid, f"旧问题{i}", answer)

    provider = RecordingProvider()
    client = TestClient(_make_store_app(tmp_path, provider, store))
    events = _stream_chat_events(client, sid, "北京天气怎么样？")
    assert events[-1]["stop_reason"] == "completed"

    assert len(provider.prompts) == 1
    prompt = provider.prompts[0]
    assert prompt.count("Q:") == 5  # 注入窗口只有最近 5 轮
    assert "旧问题1" not in prompt  # 最旧一轮被挤出窗口
    assert "旧问题6" in prompt  # 最新一轮在窗口内
    # 单条历史按 ~200 字符截断：超长回答只剩开头一段
    assert 150 <= prompt.count("长") <= 200
