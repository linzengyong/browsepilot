import asyncio
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


class FakeBrowserPool:
    """BrowserPool 替身（绝不起真浏览器）：记录 start/close，可注入启动失败。"""

    def __init__(self, *, start_error: Exception | None = None):
        self.start_error = start_error
        self.start_calls = 0
        self.close_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error

    async def close(self) -> None:
        self.close_calls += 1


class _StubServices:
    """AppToolExecutor 的 Tier1/2 服务占位：本组测试只碰浏览器槽位。"""

    async def get(self, *args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("stub service must not be called")

    async def search(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("stub service must not be called")

    async def fetch(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("stub service must not be called")


def _browser_executor(*, browser=None):
    """真 AppToolExecutor（只有 Tier3 槽位有意义）：验证 lifespan 真的绑定了池。"""
    from browsepilot.app.executor import AppToolExecutor

    stub = _StubServices()
    return AppToolExecutor(stub, stub, stub, browser=browser)


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
    # P3 Tier3：浏览器设置（默认值即接线默认行为）
    assert s.browser_headless is True
    assert s.browser_max_pages == 3
    assert s.browser_channel == ""  # F3：空 = 用 Playwright 自带 Chromium


def test_settings_browser_fields_follow_bp_env_prefix(monkeypatch):
    """BP_ 前缀约定：browser 设置与其他字段一样从环境变量读。

    包 ``browser_channel``：本机（PW 1.59，缓存只有 chromium-1208）默认 chromium 启动会报
    "Executable doesn't exist"，靠 ``BP_BROWSER_CHANNEL=msedge`` 用系统 Edge（T6 §5.6）。
    注意 v1 遗留的 ``BROWSER_CHANNEL``（无前缀）与本 Settings 无关：v2 只读 ``BP_*``。
    """
    monkeypatch.setenv("BP_BROWSER_HEADLESS", "false")
    monkeypatch.setenv("BP_BROWSER_MAX_PAGES", "5")
    monkeypatch.setenv("BP_BROWSER_CHANNEL", "msedge")
    s = Settings(_env_file=None)
    assert s.browser_headless is False
    assert s.browser_max_pages == 5
    assert s.browser_channel == "msedge"


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


# ── C1：tier3 登录墙 → /chat/resume 闭环 ────────────────────────────────────


class LoginWallBrowserExecutor(FakeExecutor):
    """browser_open 命中登录墙（D5/D6 桥的真实产出形状）；新任务还没有页面（snapshot 失败）。"""

    async def call(self, name, arguments):
        if name == "browser_snapshot":
            return ToolResult(
                status="error", error_type="no_open_page", error_message="还没有打开的页面"
            )
        if name == "browser_open":
            return ToolResult(
                status="success",
                content="已打开 https://example.com/login",
                data={
                    "action": "browser_open",
                    "url": "https://example.com/login",
                    "needs_human": True,
                    "human_prompt": "页面需要登录：请在浏览器中手动登录后继续任务。",
                    "intervention": "login",
                },
            )
        return await super().call(name, arguments)


class LoginHookedProvider(RecordingProvider):
    """记录 prompt，并模拟会被过期提示带偏的模型（P3 final review C1 的实证形状）。"""

    def __init__(self) -> None:
        super().__init__()
        self.system_prompts: list[str] = []

    async def chat(self, messages, *, model=None):
        first = messages[0] if messages else None
        self.system_prompts.append(
            first.content if first is not None and first.role == "system" else ""
        )
        await super().chat(messages, model=model)  # 记录 user prompt（prompts[-1]）
        content = messages[-1].content
        if "人工反馈" in content:
            return ChatResult(text="已基于人工反馈继续：登录后的页面已经打开。")
        if "human_prompt" in content:
            return ChatResult(text="请在浏览器中手动登录后继续任务。")
        return ChatResult(text="（没有可用上下文）")


def test_chat_resume_after_login_wall_uses_human_response(tmp_path):
    """C1 端到端（HTTP）：tier3 登录墙 → human_pending → /chat/resume 后答案基于人工反馈，
    不再要求"请手动登录"（human_response 是写-only 字段时，人工确认等于是白做）。"""
    from langgraph.checkpoint.memory import MemorySaver

    provider = LoginHookedProvider()
    app = create_app(
        Settings(_env_file=None),
        provider=provider,
        executor=LoginWallBrowserExecutor(),
        tracer=JsonlTracer(trace_id="c1-hitl", out_dir=tmp_path),
        checkpointer=MemorySaver(),
    )
    client = TestClient(app)

    # 任务文本不带 URL：带 URL 的任务按既有规则走 read_page（tier2）
    first = _stream_chat_events(client, "s1", "帮我点击页面上的登录按钮")
    assert [e["type"] for e in first] == ["phase", "human_pending"]
    assert "手动登录" in first[1]["human_prompt"]

    with client.stream(
        "POST",
        "/chat/resume",
        json={"session_id": "s1", "human_response": "我已经在浏览器里登录好了"},
    ) as resp:
        assert resp.status_code == 200
        events = [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]
    assert [e["type"] for e in events] == ["answer", "done"]
    # F3：暂停发生在**导航步**（browser_open）上 → resume 后没有任何观察（观察步从未运行），
    # 所以 stop_reason 是 partial 而不是 completed（旧断言把假成功钉成了“正确行为”）。
    assert events[1]["stop_reason"] == "partial"
    # 答案不再要求人工（旧行为：模型只看到过期的 human_prompt → 复述“请手动登录”）
    assert "手动登录" not in events[0]["text"]
    assert "已基于人工反馈继续" in events[0]["text"]
    # resume 那一轮的 prompt：人工反馈在里面，过期提示不在（system 也讲清了人工已完成）
    assert "我已经在浏览器里登录好了" in provider.prompts[-1]
    assert "人工反馈" in provider.prompts[-1]
    assert "手动登录" not in provider.prompts[-1]
    assert "不要再让用户手动登录" in provider.system_prompts[-1]


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


def test_lifespan_checkpointer_registers_msgpack_serde(tmp_path):
    """P2 MUST-FIX：lifespan 手动构造 AsyncSqliteSaver 时显式注册 msgpack serde。

    langgraph-checkpoint 4.2.0 的 allowed_msgpack_modules 只接受类型本身或
    (module, name) 符号元组（传模块名字符串是无效条目），默认 serde 为
    True=全放行+warn；未注册的 browsepilot 自有状态类型在严格模式
    （LANGGRAPH_STRICT_MSGPACK=true）下反序列化会被拒 → resume 读回裸 dict。
    断言两件事：注册集合精确，且一次 checkpoint blob 写读 roundtrip 后仍
    还原为 pydantic 实例（AgentState / 嵌套 ExecutionStep）而不是降级的 dict。
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from browsepilot.agent.state import AgentState, ExecutionStep

    ckpt_db = tmp_path / "ckpt.sqlite"
    app = create_app(
        Settings(_env_file=None, checkpoint_db_path=str(ckpt_db)),
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=JsonlTracer(trace_id="serde", out_dir=tmp_path),
    )
    with TestClient(app):
        serde = app.state.checkpointer.serde
        assert isinstance(serde, JsonPlusSerializer)
        # 显式白名单（非默认 True）：键与 langgraph 归一化后的 (module, name) 一致
        assert serde._allowed_msgpack_modules == {
            ("browsepilot.agent.state", "AgentState"),
            ("browsepilot.agent.state", "ExecutionStep"),
        }

        # checkpoint blob 写读 roundtrip：写盘用的就是 dumps_typed 的 msgpack 载荷
        state = AgentState(
            task="北京天气",
            execution_log=[ExecutionStep(index=1, tool="weather")],
            degradation_log=[
                {"strategy": "weather", "error": "city_not_found", "message": "无此城市"}
            ],
        )
        type_, blob = serde.dumps_typed(state)
        assert type_ == "msgpack"
        revived = serde.loads_typed((type_, blob))
        assert isinstance(revived, AgentState)  # 未被白名单拒绝降级成 dict
        assert revived == state


def test_lifespan_manual_saver_survives_app_restart_for_resume(tmp_path):
    """手动构造 saver（conn + 显式 serde）后生命周期语义不变：app shutdown
    关闭连接，下一次启动从同一 sqlite 文件读回 interrupt 状态并成功 resume。"""
    ckpt_db = tmp_path / "ckpt.sqlite"

    def _make(trace_id: str):
        return create_app(
            Settings(_env_file=None, checkpoint_db_path=str(ckpt_db)),
            provider=FakeProvider(),
            executor=HumanNeedingExecutor(),
            tracer=JsonlTracer(trace_id=trace_id, out_dir=tmp_path),
        )

    app1 = _make("restart-1")
    with TestClient(app1) as client:
        first = _stream_chat_events(client, "s1", "北京天气")
        assert [e["type"] for e in first] == ["phase", "human_pending"]
    assert ckpt_db.exists()

    # shutdown 必须真正关闭 app-1 的 aiosqlite 连接（aiosqlite 0.22 已无
    # is_alive()：关闭后 _running→False、底层 sqlite3 句柄置 None）。删掉
    # main.py lifespan finally 的 await conn.close() 会让此断言失败。
    app1_conn = app1.state.checkpointer.conn
    assert app1_conn._running is False
    assert app1_conn._connection is None

    # 新进程语义：重新 lifespan（新 conn/saver），从磁盘读回 interrupt 并 resume
    with TestClient(_make("restart-2")) as client:
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


# ── P3 Tier3：BrowserPool 生命周期接线（lifespan） ──────────────────────────


def test_lifespan_starts_browser_pool_and_injects_it_into_executor(tmp_path):
    """P3 收尾：lifespan 按注入的池 start() 后把（已启动的）池绑到 AppToolExecutor，
    teardown 时 close() —— 未接线时 6 个浏览器工具只能返回 browser_unavailable。

    池的配置（headless / max_pages / ``channel``）是构造期属性（F3），随这个实例一起进入
    executor：构造接线见 :func:`test_default_app_wires_browser_pool_into_executor`。
    """
    pool = FakeBrowserPool()
    executor = _browser_executor()  # 未带池：绑定必须由 lifespan 完成
    app = create_app(
        Settings(_env_file=None, checkpoint_db_path=str(tmp_path / "ckpt.sqlite")),
        provider=FakeProvider(),
        executor=executor,
        tracer=JsonlTracer(trace_id="browser-wire", out_dir=tmp_path),
        browser_pool=pool,
    )
    with TestClient(app) as client:
        assert pool.start_calls == 1  # 进入 lifespan 即启动
        assert executor._browser is pool  # 注入的是已经启动的池
        assert pool.close_calls == 0  # 运行期不回收
        assert client.get("/health").status_code == 200
    assert pool.close_calls == 1  # teardown 与 checkpointer 一起回收


def test_lifespan_browser_start_failure_degrades_to_browser_none(tmp_path):
    """启动失败只记日志：executor 的 browser 置 None（6 个工具统一
    browser_unavailable），app 照常启动 —— 浏览器故障不拖垮服务（诚实降级）。

    close() 仍被调用：池未启动时是幂等空操作，启动失败的清理由 BrowserPool.start
    自己负责（_discard_playwright）；这里用假池钉死调用次数与降级结果。
    """
    boom = FakeBrowserPool(start_error=RuntimeError("chromium executable missing"))
    # 生产形状：_default_app 先把池构造进 executor；start() 失败必须由 lifespan 摘掉
    executor = _browser_executor(browser=boom)
    app = create_app(
        Settings(_env_file=None, checkpoint_db_path=""),
        provider=FakeProvider(),
        executor=executor,
        tracer=JsonlTracer(trace_id="browser-fail", out_dir=tmp_path),
        browser_pool=boom,
    )
    with TestClient(app) as client:
        assert boom.start_calls == 1
        assert executor._browser is None  # 不把未启动的池留在 executor 上
        assert client.get("/health").status_code == 200  # app 正常启动
    assert boom.close_calls == 1


def _capture_default_app(tmp_path, monkeypatch, **settings_overrides):
    """跑 ``_default_app()`` 但只捕获它交给 create_app 的 kwargs（不起真浏览器、不跑 lifespan）。

    ``Settings`` 一并换成只看测试覆写的版本（``_env_file=None``）：本机 ``.env`` 不参与，
    否则真环境的取值会污染断言。
    """
    import browsepilot.app.main as main_module

    captured: dict = {}

    def _capture(settings, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(main_module, "create_app", _capture)
    monkeypatch.setattr(
        main_module,
        "Settings",
        lambda: Settings(
            _env_file=None,
            trace_dir=str(tmp_path / "traces"),
            session_db_path=str(tmp_path / "sessions.sqlite"),
            **settings_overrides,
        ),
    )
    main_module._default_app()
    return captured


def test_default_app_wires_browser_pool_into_executor(tmp_path, monkeypatch):
    """生产接线（gap 本身）：_default_app 按 Settings 构造 BrowserPool，以
    AppToolExecutor(..., browser=pool) 注入，并把同一实例交给 create_app 管生命周期。

    只把 create_app 换成记录器：不起真浏览器、不跑真 lifespan。
    """
    from browsepilot.app.browser.browser_pool import BrowserPool

    captured = _capture_default_app(
        tmp_path,
        monkeypatch,
        browser_headless=False,
        browser_max_pages=2,
        browser_channel="msedge",
    )

    pool = captured["browser_pool"]
    assert isinstance(pool, BrowserPool)
    assert (pool.headless, pool.max_pages) == (False, 2)  # 来自 Settings
    assert pool.channel == "msedge"  # F3：Settings.browser_channel 真的透传到池
    assert captured["executor"]._browser is pool  # 构造注入，与生命周期同源


def test_default_app_keeps_browser_pool_and_executor_on_one_settings(tmp_path, monkeypatch):
    """I3：组合根显式传 ``policy=NavigationPolicy(settings…)`` —— 不许 executor 自己再读一次环境。

    只把 create_app 换成记录器，然后用**行为**断言策略真的来自这一个 Settings：
    allowlist 模式下白名单外的域名在策略门就被拦（executor 自建默认策略是 allow_https，
    会放行 evil.com 并走到浏览器那一步变成 browser_unavailable）。
    """
    captured = _capture_default_app(
        tmp_path, monkeypatch, navigation_policy="allowlist", allowed_domains=["example.com"]
    )
    executor = captured["executor"]

    blocked = asyncio.run(executor.call("browser_open", {"url": "https://evil.com/"}))
    assert blocked.error_type == "navigation_blocked"
    assert "domain_not_allowed" in blocked.error_message
    # 白名单内：策略放行 → 卡在池（本测试没启动池），证明上面的拦截来自策略而不是“永远拒”
    allowed = asyncio.run(executor.call("browser_open", {"url": "https://www.example.com/"}))
    assert allowed.error_type == "browser_unavailable"


def test_lifespan_binds_the_pool_through_the_public_attach_browser_api(tmp_path):
    """Minor：lifespan 走 AppToolExecutor.attach_browser() 公开 API（不再写私有 ``_browser``）。

    替身只覆写公开 API：旧实现（直接赋 ``executor._browser``）下这里根本不会被调用。
    """
    pool = FakeBrowserPool()
    attached: list = []

    class AttachSpyExecutor(FakeExecutor):
        def attach_browser(self, browser):
            attached.append(browser)

    app = create_app(
        Settings(_env_file=None, checkpoint_db_path=""),
        provider=FakeProvider(),
        executor=AttachSpyExecutor(),
        tracer=JsonlTracer(trace_id="attach-api", out_dir=tmp_path),
        browser_pool=pool,
    )
    with TestClient(app) as client:
        assert attached == [pool]  # 启动后注入的是已启动的池
        assert client.get("/health").status_code == 200

    # 启动失败 → 同一个 API 传 None（不变的降级契约）
    boom = FakeBrowserPool(start_error=RuntimeError("chromium executable missing"))
    failing: list = []

    class FailingSpyExecutor(FakeExecutor):
        def attach_browser(self, browser):
            failing.append(browser)

    failing_app = create_app(
        Settings(_env_file=None, checkpoint_db_path=""),
        provider=FakeProvider(),
        executor=FailingSpyExecutor(),
        tracer=JsonlTracer(trace_id="attach-api-fail", out_dir=tmp_path),
        browser_pool=boom,
    )
    with TestClient(failing_app) as client:
        assert failing == [None]
        assert client.get("/health").status_code == 200


def test_default_app_maps_settings_channel_to_pool(tmp_path, monkeypatch):
    """F3：``Settings.browser_channel`` → ``BrowserPool(channel=...)`` 的映射规则。

    - 空串（默认）→ ``None``：``channel=""`` 不是合法值（``launch(channel="")`` 直接报错），
      空串必须收敛成 None（用 Playwright 自带 Chromium）；
    - 非空原样透传（T6 §5.6：本机 PW 1.59 默认要的 chromium rev 1217 不存在，
      真实需要系统 Edge 时用 ``BP_BROWSER_CHANNEL=msedge``）。
    """
    bundled = _capture_default_app(tmp_path, monkeypatch)
    assert bundled["browser_pool"].channel is None
    assert bundled["executor"]._browser is bundled["browser_pool"]

    channeled = _capture_default_app(tmp_path, monkeypatch, browser_channel="msedge")
    assert channeled["browser_pool"].channel == "msedge"
    assert channeled["executor"]._browser is channeled["browser_pool"]


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
