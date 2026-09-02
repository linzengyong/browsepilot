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


class HumanNeedingExecutor(FakeExecutor):
    """weather 工具返回 needs_human → 需要人工确认（HITL interrupt）。"""

    async def call(self, name, arguments):
        if name == "weather":
            return ToolResult(
                status="success",
                data={"needs_human": True, "human_prompt": "请确认城市"},
            )
        return await super().call(name, arguments)


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
    assert s.checkpoint_db_path == "data/checkpoints.sqlite"


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
        # 匿名运行也要在 phase/start 公布服务端生成的 session_id（可寻址句柄）
        assert phase["session_id"]
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


def test_anonymous_first_chat_auto_creates_session_row(tmp_path):
    """Gap 修复：空 session_id 首聊自动建会话行 —— GET /sessions 从第一轮就
    列出新会话（id == phase/start 公布的 effective session_id），且
    /history/{id} 含本轮 task/answer（前端侧栏对全新会话不再为空）。"""
    store = SessionStore(tmp_path / "s.sqlite")
    client = TestClient(_make_store_app(tmp_path, FakeProvider(), store))

    events = _stream_chat_events(client, "", "北京天气怎么样？")
    assert [e["type"] for e in events] == ["phase", "answer", "done"]
    assert events[-1]["stop_reason"] == "completed"
    sid = events[0]["session_id"]  # phase/start 公布的 effective session_id
    assert sid

    # 恰好一个新会话，行 id == 公布 id（thread_id 同值，供 checkpointer 寻址）
    sessions = client.get("/sessions").json()
    assert [s["id"] for s in sessions] == [sid]
    assert sessions[0]["thread_id"] == sid

    # /history/{id} 往返：本轮按 task/answer 落库
    resp = client.get(f"/history/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["id"] == sid
    assert [t["task"] for t in body["turns"]] == ["北京天气怎么样？"]
    assert [t["answer"] for t in body["turns"]] == ["合成回答"]

    # 第二轮沿用同一 session_id：不重复建行，只追加一轮
    # （任务带“天气”关键词，走规则路由，不经 structured fallback）
    second = _stream_chat_events(client, sid, "那上海呢？天气怎么样？")
    assert second[-1]["stop_reason"] == "completed"
    sessions = client.get("/sessions").json()
    assert [s["id"] for s in sessions] == [sid]
    body = client.get(f"/history/{sid}").json()
    assert [t["task"] for t in body["turns"]] == [
        "北京天气怎么样？",
        "那上海呢？天气怎么样？",
    ]


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


def test_chat_resume_after_interrupt(tmp_path):
    """POST /chat/resume 恢复被 HITL interrupt 暂停的任务（SSE 同 chat）。"""
    from langgraph.checkpoint.memory import MemorySaver

    checkpointer = MemorySaver()
    app = create_app(
        Settings(_env_file=None),
        provider=FakeProvider(),
        executor=HumanNeedingExecutor(),
        tracer=JsonlTracer(trace_id="api-test", out_dir=tmp_path),
        checkpointer=checkpointer,
    )
    client = TestClient(app)

    # 第一轮 /chat/stream 触发 interrupt：发 human_pending 事件并停止
    #（T5 H2：不发空 answer/done、不落库；客户端随后转 /chat/resume）
    with client.stream(
        "POST", "/chat/stream", json={"task": "北京天气", "session_id": "s1"}
    ) as resp:
        assert resp.status_code == 200
        first = [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]
    assert [e["type"] for e in first] == ["phase", "human_pending"]
    assert first[1]["human_prompt"] == "请确认城市"
    # phase/start 与 human_pending 都携带有效 session_id（客户端据此寻址 resume）
    assert first[0]["session_id"] == "s1"
    assert first[1]["session_id"] == "s1"

    # 第二轮 /chat/resume 携带人工确认 → answer + done（无 phase/start）
    with client.stream(
        "POST",
        "/chat/resume",
        json={"session_id": "s1", "human_response": "确认，就北京"},
    ) as resp:
        assert resp.status_code == 200
        events = [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]
    assert [e["type"] for e in events] == ["answer", "done"]
    assert events[0]["text"] == "合成回答"
    assert events[1]["stop_reason"] == "completed"


def test_anonymous_hitl_surfaces_generated_session_id_for_resume(tmp_path):
    """匿名会话 + HITL interrupt 不再死胡同：phase/start 先公布服务端生成的
    session_id，human_pending 复用它 —— 客户端能凭它对 /chat/resume 寻址。"""
    from langgraph.checkpoint.memory import MemorySaver

    app = create_app(
        Settings(_env_file=None),
        provider=FakeProvider(),
        executor=HumanNeedingExecutor(),
        tracer=JsonlTracer(trace_id="anon-hitl", out_dir=tmp_path),
        checkpointer=MemorySaver(),
    )
    client = TestClient(app)

    # 空 session_id → 服务端生成 uuid4；interrupt 后流在 human_pending 终止
    first = _stream_chat_events(client, "", "北京天气")
    assert [e["type"] for e in first] == ["phase", "human_pending"]
    assert first[1]["human_prompt"] == "请确认城市"
    assert first[0]["session_id"]  # phase/start 公布有效句柄
    assert first[1]["session_id"] == first[0]["session_id"]  # human_pending 复用
    sid = first[1]["session_id"]

    # 用事件公布的 session_id 恢复 —— 匿名中断从此可寻址
    with client.stream(
        "POST", "/chat/resume", json={"session_id": sid, "human_response": "确认"}
    ) as resp:
        assert resp.status_code == 200
        events = [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]
    assert [e["type"] for e in events] == ["answer", "done"]
    assert events[0]["text"] == "合成回答"
    assert events[1]["stop_reason"] == "completed"


def test_hitl_interrupt_skips_history_and_resume_records_turn(tmp_path):
    """H2+H3：interrupt 暂停不发空回答、不落库；resume 成功后按原始 task 落库。"""
    from langgraph.checkpoint.memory import MemorySaver

    store = SessionStore(tmp_path / "s.sqlite")
    sid = store.create_session("thread-h2")
    app = create_app(
        Settings(_env_file=None),
        provider=FakeProvider(),
        executor=HumanNeedingExecutor(),
        tracer=JsonlTracer(trace_id="h2", out_dir=tmp_path),
        session_store=store,
        checkpointer=MemorySaver(),
    )
    client = TestClient(app)

    first = _stream_chat_events(client, sid, "北京天气")
    assert [e["type"] for e in first] == ["phase", "human_pending"]
    assert first[1]["human_prompt"] == "请确认城市"
    assert first[0]["session_id"] == sid
    assert first[1]["session_id"] == sid
    assert client.get(f"/history/{sid}").json()["turns"] == []  # 暂停轮不落库

    with client.stream(
        "POST", "/chat/resume", json={"session_id": sid, "human_response": "确认"}
    ) as resp:
        events = [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]
    assert [e["type"] for e in events] == ["answer", "done"]
    assert events[0]["text"] == "合成回答"

    turns = client.get(f"/history/{sid}").json()["turns"]
    assert [t["task"] for t in turns] == ["北京天气"]  # 原始任务（来自 checkpoint）
    assert [t["answer"] for t in turns] == ["合成回答"]


def test_resume_recreates_missing_session_row_before_recording(tmp_path):
    """H3 落库兜底：会话行缺失（行被删但 checkpoint 仍在）时 /chat/resume 先
    重建会话行再 record_turn —— /sessions 与 /history 保持一致，不丢本轮。"""
    from langgraph.checkpoint.memory import MemorySaver

    store = SessionStore(tmp_path / "s.sqlite")
    sid = store.create_session("thread-rs")
    app = create_app(
        Settings(_env_file=None),
        provider=FakeProvider(),
        executor=HumanNeedingExecutor(),
        tracer=JsonlTracer(trace_id="resume-ensure", out_dir=tmp_path),
        session_store=store,
        checkpointer=MemorySaver(),
    )
    client = TestClient(app)

    # HITL 暂停后删掉会话行（模拟行缺失但 checkpoint 仍在的遗留状态）
    first = _stream_chat_events(client, sid, "北京天气")
    assert [e["type"] for e in first] == ["phase", "human_pending"]
    assert store.get_session(sid) is not None
    store.delete_session(sid)
    assert store.get_session(sid) is None

    with client.stream(
        "POST", "/chat/resume", json={"session_id": sid, "human_response": "确认"}
    ) as resp:
        assert resp.status_code == 200
        events = [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]
    assert [e["type"] for e in events] == ["answer", "done"]
    assert events[0]["text"] == "合成回答"

    # 行被重建 + 本轮按原始 task 落库；/sessions 与 /history 一致
    assert store.get_session(sid) is not None
    sessions = client.get("/sessions").json()
    assert [s["id"] for s in sessions] == [sid]
    turns = client.get(f"/history/{sid}").json()["turns"]
    assert [t["task"] for t in turns] == ["北京天气"]  # 原始任务（来自 checkpoint）
    assert [t["answer"] for t in turns] == ["合成回答"]


def test_new_chat_on_pending_thread_rejected_with_clear_error(tmp_path):
    """H4：已有待人工确认的线程上再发 /chat/stream → 明确报错，不静默 resume。"""
    from langgraph.checkpoint.memory import MemorySaver

    app = create_app(
        Settings(_env_file=None),
        provider=FakeProvider(),
        executor=HumanNeedingExecutor(),
        tracer=JsonlTracer(trace_id="h4", out_dir=tmp_path),
        checkpointer=MemorySaver(),
    )
    client = TestClient(app)

    first = _stream_chat_events(client, "s1", "北京天气")
    assert [e["type"] for e in first] == ["phase", "human_pending"]
    assert first[0]["session_id"] == "s1"
    assert first[1]["session_id"] == "s1"

    blocked = _stream_chat_events(client, "s1", "上海天气")
    assert [e["type"] for e in blocked] == ["phase", "error", "done"]
    assert "pending human action" in blocked[1]["message"]
    assert blocked[-1]["stop_reason"] == "error"

    # 线程仍可正常 resume
    with client.stream(
        "POST", "/chat/resume", json={"session_id": "s1", "human_response": "确认"}
    ) as resp:
        events = [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]
    assert [e["type"] for e in events] == ["answer", "done"]
    assert events[0]["text"] == "合成回答"


def test_lifespan_wires_checkpointer_from_settings(tmp_path):
    """H1：未显式传 checkpointer 时，lifespan 按 checkpoint_db_path 创建共享
    AsyncSqliteSaver —— interrupt 状态跨请求存活，shutdown 时回收。"""
    ckpt_db = tmp_path / "ckpt.sqlite"
    app = create_app(
        Settings(_env_file=None, checkpoint_db_path=str(ckpt_db)),
        provider=FakeProvider(),
        executor=HumanNeedingExecutor(),
        tracer=JsonlTracer(trace_id="h1", out_dir=tmp_path),
    )
    with TestClient(app) as client:
        # 第一轮 interrupt（checkpointer 由 lifespan 创建并跨请求共享）
        first = _stream_chat_events(client, "s1", "北京天气")
        assert [e["type"] for e in first] == ["phase", "human_pending"]
        assert first[1]["human_prompt"] == "请确认城市"
        assert first[0]["session_id"] == "s1"
        assert first[1]["session_id"] == "s1"

        # 空 session_id → 服务端生成 uuid4 thread_id，正常跑完整流程
        empty = _stream_chat_events(client, "", "你好")
        assert [e["type"] for e in empty] == ["phase", "answer", "done"]
        assert empty[1]["text"] == "合成回答"
        # 匿名运行的 phase/start 公布生成的 uuid4，且不与具名会话混淆
        assert empty[0]["session_id"]
        assert empty[0]["session_id"] != "s1"

        # resume 跨请求恢复 → 证明 saver 在整个 app 生命周期存活
        with client.stream(
            "POST", "/chat/resume", json={"session_id": "s1", "human_response": "确认"}
        ) as resp:
            events = [
                json.loads(line[len("data: ") :])
                for line in resp.iter_lines()
                if line.startswith("data: ")
            ]
        assert [e["type"] for e in events] == ["answer", "done"]
        assert events[0]["text"] == "合成回答"
    assert ckpt_db.exists()  # checkpoint 已落盘


def test_resume_without_pending_interrupt_errors(tmp_path):
    """无待处理 interrupt 时 resume 必须明确报错，而不是静默开新任务。"""
    from langgraph.checkpoint.memory import MemorySaver

    app = create_app(
        Settings(_env_file=None),
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=JsonlTracer(trace_id="api-test", out_dir=tmp_path),
        checkpointer=MemorySaver(),
    )
    client = TestClient(app)
    with client.stream(
        "POST", "/chat/resume", json={"session_id": "s1", "human_response": "确认"}
    ) as resp:
        assert resp.status_code == 200
        events = [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]
    types = [e["type"] for e in events]
    assert "error" in types
    assert types[-1] == "done"
    assert events[-1]["stop_reason"] == "error"
    assert "answer" not in types
    assert "no pending human action" in events[types.index("error")]["message"]


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
