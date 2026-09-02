"""P2 Task 7: sliding-window rate limiter + HTTP 429 guard + session token budget.

- SlidingWindowRateLimiter：内存滑动窗口、thread-safe（单实例；scale 换 Redis）。
- HTTP 中间件只保护生成型 chat 端点（/chat/stream、/chat/resume），按客户端
  IP 限流 → 429 + Retry-After；create_app 默认不注入 limiter（单测旁路），
  生产接线 _default_app 按 Settings 构造。
- 会话预算：已记录 turns 数 × 4000 估算 > session_budget_tokens → 429 明确
  报错（估算性质，P4 换精确 token 统计）。
"""

import json
import time

from fastapi.testclient import TestClient

from browsepilot.agent.tools import ToolResult, ToolSpec
from browsepilot.agent.tracer import JsonlTracer
from browsepilot.app.config import Settings
from browsepilot.app.main import create_app
from browsepilot.app.rate_limit import SlidingWindowRateLimiter
from browsepilot.app.sessions import SessionStore
from browsepilot.model.messages import ChatResult

# ── Limiter 单元测试 ───────────────────────────────────────────────────────


def test_rate_limiter_allows_up_to_max():
    rl = SlidingWindowRateLimiter(max_requests=3, window_seconds=60)
    assert rl.allow("ip1") and rl.allow("ip1") and rl.allow("ip1")
    assert not rl.allow("ip1")
    assert rl.allow("ip2")  # independent key


def test_rate_limiter_window_expires(monkeypatch):
    # 可控时钟替代 brief 原稿的 self-recursive lambda（monkeypatch 后
    # `lambda: time.monotonic() + 61` 会无限递归）——语义不变：拨快 61s
    # 使窗口内最早命中过期后恢复放行。
    now = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    rl = SlidingWindowRateLimiter(max_requests=1, window_seconds=60)
    assert rl.allow("ip1")
    assert not rl.allow("ip1")
    now += 61
    assert rl.allow("ip1")


# ── Settings 字段 ──────────────────────────────────────────────────────────


def test_settings_rate_limit_defaults(monkeypatch):
    s = Settings(_env_file=None)
    assert s.rate_limit_requests == 60
    assert s.rate_limit_window_seconds == 60
    assert s.session_budget_tokens == 200000
    # BP_ 前缀环境变量可覆盖
    monkeypatch.setenv("BP_RATE_LIMIT_REQUESTS", "5")
    assert Settings(_env_file=None).rate_limit_requests == 5


# ── HTTP 层（中间件 + 预算闸门）────────────────────────────────────────────


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


def _make_app(tmp_path, *, settings=None, store=None, limiter=None):
    return create_app(
        settings or Settings(_env_file=None),
        provider=FakeProvider(),
        executor=FakeExecutor(),
        tracer=JsonlTracer(trace_id="rate-limit-test", out_dir=tmp_path),
        session_store=store,
        rate_limiter=limiter,
    )


def _stream_events(client, path: str, payload: dict) -> list[dict]:
    """POST 并完整消费 SSE 响应体（事件断言必须发生在流内）。"""
    with client.stream("POST", path, json=payload) as resp:
        assert resp.status_code == 200
        return [
            json.loads(line[len("data: ") :])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]


def test_chat_stream_429_after_max_per_client_ip(tmp_path):
    """限流中间件：同一客户端 IP 超过 max_requests → 429 + Retry-After。"""
    app = _make_app(
        tmp_path,
        limiter=SlidingWindowRateLimiter(max_requests=2, window_seconds=60),
    )
    client = TestClient(app)
    for _ in range(2):
        events = _stream_events(client, "/chat/stream", {"task": "你好"})
        assert events[-1]["type"] == "done"

    resp = client.post("/chat/stream", json={"task": "你好"})
    assert resp.status_code == 429
    assert resp.json() == {"detail": "rate limit exceeded"}
    assert resp.headers["retry-after"] == "60"  # settings.rate_limit_window_seconds

    # 窗口未过期前继续 429；非受保护端点（/health）不受影响
    assert client.post("/chat/stream", json={"task": "你好"}).status_code == 429
    assert client.get("/health").status_code == 200


def test_chat_resume_path_rate_limited_too(tmp_path):
    """/chat/resume 同样受中间件保护（deny-all limiter → 429），/health 不受影响。"""
    app = _make_app(
        tmp_path,
        limiter=SlidingWindowRateLimiter(max_requests=0, window_seconds=60),
    )
    client = TestClient(app)
    resp = client.post(
        "/chat/resume", json={"session_id": "s1", "human_response": "确认"}
    )
    assert resp.status_code == 429
    assert resp.json()["detail"] == "rate limit exceeded"
    assert client.get("/health").status_code == 200


def test_no_limiter_by_default_in_create_app(tmp_path):
    """create_app 默认不注入 limiter → 不限流（既有测试契约不受破坏）。"""
    app = _make_app(tmp_path)
    client = TestClient(app)
    for _ in range(3):
        events = _stream_events(client, "/chat/stream", {"task": "你好"})
        assert [e["type"] for e in events] == ["phase", "answer", "done"]


def _budget_app(tmp_path, store, budget_tokens):
    return _make_app(
        tmp_path,
        settings=Settings(_env_file=None, session_budget_tokens=budget_tokens),
        store=store,
    )


def test_chat_stream_429_when_session_budget_exceeded(tmp_path):
    """会话预算闸门：已记录 turns × 4000 估算超预算 → 429 + 明确 detail。"""
    store = SessionStore(tmp_path / "s.sqlite")
    sid = store.create_session("thread-budget")
    for i in range(3):
        store.record_turn(sid, f"q{i}", "a")
    client = TestClient(_budget_app(tmp_path, store, budget_tokens=8000))

    resp = client.post("/chat/stream", json={"task": "你好", "session_id": sid})
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert "budget" in detail
    assert "12000" in detail  # 3 turns × 4000 估算值（锁定估算公式）
    assert "8000" in detail  # 预算上限


def test_chat_stream_allowed_within_session_budget(tmp_path):
    """预算内会话与匿名会话（无 session_id）不受预算闸门影响。"""
    store = SessionStore(tmp_path / "s.sqlite")
    sid = store.create_session("thread-ok")
    store.record_turn(sid, "q", "a")  # 1 turn × 4000 = 4000 ≤ 8000
    client = TestClient(_budget_app(tmp_path, store, budget_tokens=8000))

    events = _stream_events(client, "/chat/stream", {"task": "你好", "session_id": sid})
    assert events[-1]["type"] == "done"
    assert events[-2]["type"] == "answer"

    # 匿名（无 session_id）跳过预算检查
    events = _stream_events(client, "/chat/stream", {"task": "你好"})
    assert events[-1]["type"] == "done"
