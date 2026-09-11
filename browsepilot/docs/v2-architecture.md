# BrowsePilot V2.0 架构文档

> **本文档由 P5 Task 4 编写（2026-09-11，HEAD `7304bee`）**。机制、字段名、数字**逐字取自当前代码**
> （`src/browsepilot/`、`eval/`、`tests/`）与设计文档 `docs/superpowers/specs/2026-08-02-browsepilot-v2-design.md`；
> 未实现/未验证的内容显式标注（§6）。相关文档：部署 `docs/v2-deploy.md` · 评测 `docs/v2-eval.md` ·
> 性能 `docs/v2-performance.md`。本文档写的是**实现现状**；"为什么这样设计"以 spec 为准（§5 的 D1–D8 表逐字引自 spec §3.2）。
>
> 覆盖范围：三层架构与端口 · 一次请求的数据流 · agent 图与策略链 · 降级/HITL/会话/Tracer/Tier3 机制 ·
> 关键决策 D1–D8 · 分层守卫与目录结构。

## 1. 三层架构（model ← agent ← app）

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ browsepilot/app —— 交付与组装（FastAPI 组合根 + Tier1/2/3 能力实现）           │
│   Web API + SSE：/chat/stream · /chat/resume · /sessions · /history · /health  │
│   AppToolExecutor（工具分发）· app.browser（池/动作/策略/检测/快照）           │
│   app.services（weather / search / fetch）· SessionStore · 限流中间件          │
│   Settings（BP_* 环境变量）· A2A Server：spec D6 预留 —— 未实现（§6）          │
├────────────────────────────────────────────────────────────────────────────────┤
│ browsepilot/agent —— 通用 Agent 循环（只依赖 model 层 + 端口）                 │
│   LangGraph 图：classify → execute →（hitl）→ answer                           │
│   router（规则优先意图路由）· state（Pydantic AgentState）· params（参数模型） │
│   端口：ToolExecutor（看不见 MCP/Playwright）· Tracer（JSONL 默认 sink）       │
├────────────────────────────────────────────────────────────────────────────────┤
│ browsepilot/model —— 模型适配层（只依赖第三方库 + 标准库）                     │
│   ModelProvider 端口：chat() · stream() · structured()                         │
│   LiteLLMProvider（LiteLLM 底座）· Message / Usage / Cost / ChatResult         │
├────────────────────────────────────────────────────────────────────────────────┤
│ 基础设施：Playwright（Tier3）· SQLite（会话元数据 + LangGraph checkpoint）     │
│           外部 API：Open-Meteo（天气）· ddgs（搜索）· 目标站点 HTTP            │
└────────────────────────────────────────────────────────────────────────────────┘
```

依赖方向 `app → agent → model` **严格单向**（由 `tests/test_architecture.py` 的 3 条守卫静态强制，见 §5.1）。
跨层通信只能走三个端口（§1.2）：`ModelProvider`（agent→model）、`ToolExecutor` / `Tracer`（由 app 实现、注入 agent 使用）。

### 1.1 各层职责与文件

| 层 | 职责 | 关键文件 |
|----|------|----------|
| model | 统一消息/用量/成本模型；LiteLLM 适配；结构化输出（forced tool_choice + Pydantic + 重试） | `model/messages.py`（`Message`/`Usage`/`Cost`/`UsageInfo`/`ChatResult`）· `model/provider.py`（`ModelProvider` 协议）· `model/litellm_provider.py`（`LiteLLMProvider`、`to_openai_messages`）· `model/structured.py`（`StructuredOutputError`、`schema_for`、`tool_spec_for`） |
| agent | 意图路由、图编排（分类/执行/人工/回答）、Pydantic 状态、参数模型、端口定义、任务入口 | `agent/router.py` · `agent/graph.py`（`build_graph`、`_STRATEGY_CHAIN`）· `agent/state.py` · `agent/params.py` · `agent/tools.py`（`ToolExecutor`/`ToolSpec`/`ToolResult`）· `agent/tracer.py`（`Tracer`/`TraceEvent`/`JsonlTracer`）· `agent/tracer_provider.py`（`TracingModelProvider`）· `agent/api.py`（`run_task`/`resume_task`/`has_pending_human_action`） |
| app | HTTP/SSE 契约、组合根与生命周期、工具实现（Tier1/2/3 服务与浏览器）、会话持久化、限流/预算、配置 | `app/main.py`（`create_app`/`_default_app`）· `app/executor.py`（`AppToolExecutor`）· `app/config.py`（`Settings`）· `app/sessions.py`（`SessionStore`）· `app/rate_limit.py` · `app/browser/{browser_pool,actions,policy,detect,snapshot}.py` · `app/services/{weather,search,fetch,city_coords}.py` |

### 1.2 端口（Protocol，全部 `runtime_checkable`）

实现层之间唯一的耦合点；三个端口的签名逐字取自代码：

```python
# browsepilot/model/provider.py —— model 层唯一公开入口
class ModelProvider(Protocol):
    async def chat(self, messages: list[Message], *, model: str | None = None) -> ChatResult: ...
    def stream(self, messages: list[Message], *, model: str | None = None) -> AsyncIterator[str]: ...
    async def structured(
        self, messages: list[Message], *, schema: type[BaseModel],
        model: str | None = None, max_retries: int = 3,
    ) -> BaseModel: ...

# browsepilot/agent/tools.py —— agent 层看向外界的唯一窗口
class ToolExecutor(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...
    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...

# browsepilot/agent/tracer.py —— agent 的"黑匣子"，落到哪由 sink 决定
class Tracer(Protocol):
    trace_id: str
    def emit(self, event: TraceEvent) -> None: ...
    def span(self, node: str, **data: Any) -> AbstractContextManager[TraceSpan]: ...
    def new_span_id(self) -> str: ...
```

当前实现：`ModelProvider` → `LiteLLMProvider`（app 组合根注入）；`ToolExecutor` → `AppToolExecutor`（生产）/ `eval.fake_deps.FakeExecutor`（mock 评测）；`Tracer` → `JsonlTracer`（唯一 sink；spec D3 声明 Langfuse/LangSmith 为可选实现，**未接**）。

### 1.3 目录结构

```
browsepilot/
├── src/browsepilot/            # 唯一 Python 实现（v1 backend/ + browser_mcp/ 已于 P5-T1 删除）
│   ├── model/                  # messages · provider · litellm_provider · structured
│   ├── agent/                  # api · graph · params · router · state · tools · tracer · tracer_provider
│   └── app/                    # main · config · executor · sessions · rate_limit
│       ├── browser/            # actions · browser_pool · detect · policy · snapshot
│       └── services/           # weather · search · fetch · city_coords
├── eval/                       # 评测：scenarios.py/schemas · scenarios/*.yaml(16) · checks · harness · report · run_eval · bench · gate.sh
├── tests/                      # test_agent/ · test_app/ · test_eval/ · test_model/ · test_architecture.py
├── frontend-vue/               # Vue3 + Vite 演示层（:5173，/api 代理 → :8001）
├── docs/                       # v2-eval.md · v2-performance.md · 本文件 · v2-deploy.md · 阶段报告
├── data/                       # traces / sessions / checkpoints / eval-reports（**gitignored**）
├── skills/                     # v1 遗留，v2 代码零引用（grep 验证）；去留待决策
├── Dockerfile · docker-compose.yml · .dockerignore
├── start.sh · start.bat        # 一键启动后端 8001 + 前端 5173（P5-T1 改写）
└── pyproject.toml · uv.lock    # uv 唯一入口；hatch packages = ["src/browsepilot"]
```

## 2. 数据流：一次任务的完整路径

以 `POST /chat/stream`（最简单路径 + HITL 分支）为例，逐段对应代码：

| # | 阶段 | 位置 | 行为 |
|---|------|------|------|
| 1 | HTTP 入口 | 前端 `frontend-vue/src/api/chat.ts` | `POST /api/chat/stream`（Vite 代理把 `/api` 重写到 `http://127.0.0.1:8001`）；SSE 帧形状 = 扁平 `data: {"type": ...}` |
| 2 | 限流 | `app/main.py` middleware | 只拦 `("/chat/stream", "/chat/resume")`，按客户端 IP 走 `SlidingWindowRateLimiter`；超限 → **429 + `Retry-After: <窗口秒数>`** |
| 3 | 会话准备 | `chat_stream` | `effective_session_id = req.session_id or uuid4`；`session_store` 里有行则复用、没有则建行（匿名首聊同样落行）；**预算闸门**：已记录 turns × 4000 估算 token > `BP_SESSION_BUDGET_TOKENS` → 429（估算性质，见 §4.4） |
| 4 | SSE 开流 | `StreamingResponse(gen(), media_type="text/event-stream")` | 先发 `{"type":"phase","phase":"start","task":…,"session_id":…}`（公布 thread_id，供 `/chat/resume` 寻址） |
| 5 | H4 守卫 | `has_pending_human_action` | 显式 session_id 且线程暂停在 HITL → 发 `{"type":"error",…}` + `{"type":"done","stop_reason":"error"}`，**不**静默开新任务 |
| 6 | 跑图 | `agent/api.py::run_task` | `build_graph(executor, tracer, TracingModelProvider(provider, tracer, session_id), checkpointer=…)` 后 `graph.ainvoke(AgentState(task, session_id, history), config={"configurable":{"thread_id": effective_session_id}})` |
| 7 | classify | `agent/graph.py` | 先 `classify_by_rules(task)`；规则未命中 → `provider.structured(IntentResult)`；结构化失败 → 兜底 `search/tier1`。发 `decision` trace 事件；state 写 `intent`/`tier` |
| 8 | 路由 | `_route_after_classify` | `chitchat`/`knowledge_qa` → 直接 `answer`；其余 → `execute` |
| 9 | execute | `_STRATEGY_CHAIN[intent]` 循环 | 每个工具：`provider.structured(<Params 模型>)` 提参数（无参工具跳过）→ `executor.call` → 成功即收尾；失败写 `degradation_log` 后试下一个。发 `tool_call` trace 事件 |
| 10 | 工具实现 | `AppToolExecutor.call` | `weather`→`WeatherService`（内置城市表 + Open-Meteo）；`search`→`SearchService`（ddgs）；`fetch_page`→`PageFetchService`（httpx + trafilatura）；`browser_*`→`BrowserPool` 页面 + `app.browser.actions`（策略门/检测/复查，见 §4.6） |
| 11 | HITL 分支 | `graph.execute` | 工具成功但 `data["needs_human"]` → 置 `pending_human_action=True` + `human_prompt` → `_route_execute_result` 进 `hitl` 节点 `interrupt()`；SSE 发 `{"type":"human_pending","session_id":…,"human_prompt":…}` 后结束（**不落库**） |
| 12 | answer | `graph.answer` | 数据路径：`provider.chat([system, user(任务 + JSON payload + partial 注记)])`；全败路径：模板文案；直答路径：`provider.chat`（chitchat/knowledge_qa，带 history）。产出 `final_answer` + `stop_reason`（`completed` / `partial`） |
| 13 | 落库 | `chat_stream` | `session_store.record_turn(session_id, task, final_answer)`（失败只记日志，不破坏 SSE 契约） |
| 14 | SSE 收尾 | `chat_stream` | `{"type":"answer","text":…}` + `{"type":"done","stop_reason":…,"result_data":…}`；异常路径发 `error` + `done(stop_reason="error")`（异常不冒泡给客户端） |

`/chat/resume` 的路径：SSE 开流（无 `phase/start`）→ `resume_task`（先 `aget_state` 校验 `snapshot.next` 与 `pending_human_action`，否则 `RuntimeError` → error/done）→ `Command(resume=human_response)` 续跑 `hitl → answer` → 落库（task 取自 checkpoint state values）→ answer/done。

Tracer 旁路：`TracingModelProvider` 把每次 chat/stream/structured 记成 `llm_call`；graph 节点用 `tracer.span(...)` 记 `node_start`/`node_end`；写入 `BP_TRACE_DIR`（默认 `data/traces/`）。

## 3. agent 图与策略链

### 3.1 图结构（`agent/graph.py::build_graph`）

| 节点 | 入口/边 | 说明 |
|------|---------|------|
| `classify` | `set_entry_point` | 规则优先分类（§4.1）；发 `decision` 事件 |
| `execute` | `classify --非直答--> execute` | 策略链循环 + 降级记录（§3.2 / §4.2） |
| `hitl` | `execute --pending_human_action 且 hitl_triggered_count==0--> hitl` | `interrupt({"human_prompt": …})`；resume 后只回 answer（§4.3） |
| `answer` | `hitl --> answer`；`classify --chitchat/knowledge_qa--> answer`；`execute --其余--> answer` | 合成回答 + `stop_reason`；`answer --> END` |

判定函数：`_route_after_classify` = `"answer" if intent in ("chitchat","knowledge_qa") else "execute"`；
`_route_execute_result` = `"hitl" if pending_human_action and hitl_triggered_count == 0 else "answer"`。

### 3.2 `_STRATEGY_CHAIN`（逐字取自 `graph.py`）

| intent | 链 | 语义 |
|--------|----|------|
| `weather` | `["weather", "search"]` | 城市未命中 → 用**任务原文**作 query 搜索兜底（`SearchParams(query=state.task)`，不再结构化提取） |
| `search` | `["search"]` | 无有意义的回退，失败即 `all_strategies_failed` |
| `read_page` | `["fetch_page", "browser_open"]` | HTTP 正文提取失败 → 升级到真浏览器打开同一 URL |
| `browser_task` | `["browser_open", "browser_snapshot"]` | 先导航后观察（`_CONTINUE_AFTER = {"browser_open": "browser_snapshot"}`）；open 成功不算终点 |
| （未列出的 intent） | `["search"]` | `_STRATEGY_CHAIN.get(state.intent, ["search"])` 的缺省 |

参数提取 `_PARAM_MODELS`：`weather→WeatherParams(city)`、`search→SearchParams(query)`、`fetch_page→ReadPageParams(url)`、
`browser_open/click/type/scroll/execute_js→BrowserParams(url,target,text,direction,expression)`、**`browser_snapshot→None`**（无参工具不花模型调用）。
参数模型定义在 `agent/params.py`；executor 每个工具只取自己要的字段（`ref`/`target` 是别名）。

## 4. 关键机制

### 4.1 意图路由（规则优先混合策略，spec D4 / article 3）

`classify_by_rules` 的**规则顺序即优先级**（`router._RULES`，逐字）：

| 顺序 | 判定 | intent → tier |
|------|------|---------------|
| 0 | 空文本直接返回 | `chitchat` → `answer` |
| 0' | URL 正则 `https?://` 命中（先于全部规则） | `read_page` → `tier2` |
| 1 | 天气/气温/温度/多少度/下雨/下雪/晴/阴/weather/台风/空气质量 | `weather` → `tier1` |
| 2 | 打开/访问/读/阅读/摘要 | `read_page` → `tier2` |
| 3 | 解释/什么是/定义/区别/原理/如何理解 | `knowledge_qa` → `answer` |
| 4 | `_is_browser_task`：强词（电商/站点词 + `搜索框`/`浏览器`）直接命中；弱词（点击/填写/输入/提交/勾选/选择/登录/注册/表单）须与页面上下文（`页面|按钮|表单|框(?!架)|网站|<站点词>`）共现，且不含求知措辞（怎么/如何/为什么/教程/规范/指南/入门） | `browser_task` → `tier3` |
| 5 | 搜索/搜一下/查一下/帮我找/search/是什么/什么时候/为什么/怎么样 | `search` → `tier1` |
| 6 | 你好/嗨/hello/hi/谢谢/再见/你是谁 | `chitchat` → `answer` |

规则全不命中 → `None` → graph 调 `provider.structured(IntentResult)`（模型兜底，`confidence=0.8`）；
结构化失败 → `RouteDecision(intent="search", tier="tier1", confidence=0.0)`（"search is the safest default"）。
`IntentResult` 的 `intent`/`tier` 是 Literal（拼错在模型侧就报错）。

### 4.2 降级（degradation_log / 兜底 / `degraded_from` / partial）

- **记录形状**：每次失败 `{"strategy", "error", "message"}`；参数提取失败 → `error="param_extraction_failed"`；工具失败 → `tool_result.error_type`/`error_message`（如 `city_not_found`、`navigation_blocked`、`browser_unavailable`、`timeout`）。
- **合并去重**：`_merge_degradation` 按 `(strategy, error)` 去重、保留首次出现 —— resume 会**重放** interrupt 之前的 `execute` 节点，不去重会让同一条记录翻倍。
- **全败兜底**：`result_data = {"error": "all_strategies_failed", "degradation": [...]}`；answer 逐条渲染成人话：`"抱歉，这个任务暂时无法完成：{strategy}: {message}；…。可以换个说法试试。"`（绝不编造）。
- **降级成功后仍拿到数据**（weather→search 兜底成功）：`_degraded_provenance` 把去重后的失败尝试注入 payload 的 `degraded_from`，并拼上 `_DEGRADED_ANSWER_RULE`（spec §2「带说明的降级回答」）—— 要求模型①说明原始尝试失败与原因、②说明数据来自哪条兜底路径、③不得把兜底结果的实体/数值当作原问题的答案。
- **partial（第三种结果）**：`stop_reason = "partial" if result_data.get("partial") else "completed"`；稳定原因码：`no_navigation_for_task`（没导航就观察）、`no_observation_after_navigation`（导航成功但观察失败）、`hitl_no_observation`（HITL 暂停在导航步、resume 后观察步不会跑）；answer 的 user 消息带 `_partial_note` 要求模型说清"已看到什么、还有什么没完成"。
- **`browser=None`（Tier3 未接线）**：6 个浏览器工具统一返回 `browser_unavailable`，图不特判 —— 走既有降级链到 `all_strategies_failed` + 明确告知。

### 4.3 HITL（interrupt / resume / checkpointer）

- **触发**：工具**成功**结果里带 `needs_human=True`（来自 `browser.detect` 的 high 置信命中）；medium 只记 `data["intervention"]` + 日志、**不暂停**（每次运行只有一次人工暂停机会：`_route_execute_result` 在 `hitl_triggered_count > 0` 后不再中断）。
- **暂停**：`hitl` 节点 `interrupt({"human_prompt": state.human_prompt})`；state 置 `pending_human_action=True`（`human_response`/`hitl_triggered_count` 在 resume 后由该节点写回）。失败/错误路径**不会**置位（错误继续走降级链，绝不把失败伪装成"需要人工"）。
- **checkpoint**：app lifespan 按 `BP_CHECKPOINT_DB_PATH` 创建**共享** `AsyncSqliteSaver`（手动构造 `aiosqlite` conn，`JsonPlusSerializer(allowed_msgpack_modules=(AgentState, ExecutionStep))` 白名单，避免 resume 读回裸 dict）；`create_app` 显式传入的 checkpointer 由调用方管理（测试）。
- **resume**：`POST /chat/resume`（SSE）→ `resume_task` 先校验 `snapshot.next` 非空且 `pending_human_action` 为真（否则 `RuntimeError`，不静默开新任务）→ `graph.ainvoke(Command(resume=human_response), config={"configurable":{"thread_id": session_id}})`；返回时额外带回原始 `task`（取自 checkpoint values）供落库。
- **resume 语义**：只回到 `answer` —— 链上还没跑的步骤（典型是导航步之后的观察步）**不会**自动继续，所以暂停在导航步上时数据标 `partial`（`hitl_no_observation`），想拿页面内容得下一轮任务重新发起。
- **answer 的控制面剥离**：`_answer_payload` **无条件**剥掉 `needs_human`；有 `human_response` 时再剥 `human_prompt`/`intervention` 并注入 `human_response`；system 追加"人工已经完成了……不要再让用户手动登录"。
- **并发守卫（H4）**：线程已暂停时对同一 session_id 发新的 `/chat/stream` → error/done，提示改走 `/chat/resume`。

### 4.4 会话与多轮（SessionStore / thread_id / history）

- **存储**：`SessionStore`（stdlib `sqlite3`，无 ORM）建两张表 —— `sessions(id, thread_id, name, pinned, created_at, updated_at)` 与 `turns(id AUTOINCREMENT, session_id, task, answer, created_at)`；库路径 `BP_SESSION_DB_PATH`（默认 `data/sessions.sqlite`）。
- **thread_id 约定**：`effective_session_id = req.session_id or uuid4`，同时用作 LangGraph `thread_id` 与 sessions 行 id（匿名首聊在 `phase/start` 里公布，客户端凭它做 `/chat/resume`、`/history` 寻址）。客户端显式带 `session_id` 时行 id 就用它 —— 同一 id 的后续请求复用同一会话/线程（会话行缺失时才新建）。
- **history 注入**：`_build_history_entries(turns)` → 最近 5 条（`_HISTORY_MAX_ENTRIES`）、每条 `"Q: <task> / A: <answer>"` 压单行、截断 200 字符（`_HISTORY_MAX_ENTRY_CHARS`）；answer 节点再按 `history[-5:]` 兜底。直答路径与数据路径都注入；**全败模板路径不注入**（未触发缺口，§6）。
- **REST**：`GET /sessions`（pinned 优先、updated_at 倒序）· `PATCH /sessions/{id}/rename` · `PATCH /sessions/{id}/pin` · `DELETE /sessions/{id}` · `GET /history/{session_id}`（404 = 会话不存在）；无 store 时这些端点 503。
- **预算**：`turns × 4000`（`_EST_TOKENS_PER_TURN`）估算会话累计 token > `BP_SESSION_BUDGET_TOKENS` → 启动前 429（估算性质；精确 token 统计未接）。
- **限流**：`SlidingWindowRateLimiter` 内存滑动窗口（单实例；scale 时换 Redis 实现），只保护两个生成型端点。

### 4.5 Tracer（事件类型 / JSONL）

- **事件模型**（`TraceEvent`）：`trace_id` · `span_id` · `parent_span_id` · `node` · `event_type` · `timestamp`（UTC ISO）· `data` · `duration_ms`。
- **event_type 取值**（注释即契约）：`node_start` | `node_end` | `tool_call` | `llm_call` | `decision` | `error` | `info`（缺省 `info`）。
- **span 语义**：一个 span 一个 `span_id`，`node_start`/`node_end` 共享；嵌套靠 per-instance `contextvars` 栈（任务局部、async 安全），因此同一 tracer 上并发跑图不会串 parent。
- **当前发点**：`span()` 包 `classify`/`execute`/`hitl`/`answer`；`classify` 发 `decision`（决策 dict）；`execute` 每次工具调用发 `tool_call`（`{"tool", "status"}`）；`TracingModelProvider` 每次 chat/stream/structured 发 `llm_call`（`{"kind", "model", "session_id"}` + `duration_ms`）。
- **sink**：`JsonlTracer` 一 trace 一文件（append-only JSONL，线程锁保护）→ `<BP_TRACE_DIR>/<trace_id>.jsonl`。
- **进程级单例（实现现状）**：`_default_app()` 只创建一个 `JsonlTracer`（无 trace_id → 生成一个 uuid4），所以**一个进程一个 trace_id / 一个 JSONL 文件**，所有请求追加到同一文件；`session_id` 只出现在 `llm_call` 事件的 data 里。按会话/任务拆文件未实现（§6）。

### 4.6 Tier3 浏览器层（app.browser，自包含）

- **BrowserPool**：一个 Chromium + 至多 `max_pages` 张 page；`start()`（幂等）→ `new_page()`（复用 idle，到上限回收最老的 busy page）→ `release()`（回池）→ `discard()`（关闭、**不回池**）→ `close()`（幂等）；`new_page()` 交出 idle 页前先看 URL —— 停在浏览器错误页（`chrome-error://`）就弃用换新页（错误页的导航可能还在飞，复用会让下一次 `goto` 报 "interrupted by another navigation"）。计数：`pages_created/reused/evicted/discarded`。
- **动作原语**（`actions.py`）：`navigate`（`goto(wait_until="domcontentloaded")` + **有界** `networkidle` 等待，`DEFAULT_WAIT_MS=500`，超时不判失败）· `click`（`ACTION_TIMEOUT_MS=5000`、`ACTION_RETRIES=2`、`MAX_RETRIES=5`）· `type_text`（先 `fill("")` 再填，重放幂等）· `scroll`；定位回退 `aria-ref=eN` → CSS → 文本，候选序逐个试，最后手段 JS（click `el => el.click()`）；所有异常转结构化错误 dict（失败即数据，不向图抛异常）。
- **URL 安全策略**（`policy.py`，spec D4/D7）：`allow_https`（默认）只放行 `http`/`https`，拒 `file:`/`javascript:`/`data:`/`ftp:`/`about:`/`chrome:`/`blob:` 等，拒字面私有/回环/链路本地/保留地址与本地主机名/单标签主机名/整数形式 IPv4，主机按 WHATWG 语义归一化（`\`↔`/`、UTS46 子集）后再判定；`allowlist` 模式在 scheme 门禁之上只放行 `allowed_domains`（精确或子域后缀，**不支持通配符**；空名单 = 全拒 `allowlist_empty`，构造不抛错）。策略在导航**之前**过门（`browser_open` → `validate`）。
- **动作后复查 + 入口守卫**（`executor._classify_page_url` 统一顺序：浏览器错误页 → 空白页 → 策略）：四处动作后复查（`browser_open` 成功、`click`、`type`、`execute_js` 成功与失败），外加 `browser_snapshot` 的入口复查；命中违规/错误页即 `discard` 该页（不回 idle），用户必须重新 `browser_open` 过策略门。
- **HITL 检测**（`detect.py`，D5/D6）：只吃**渲染后可见文本**（`body.inner_text()` 前 20KB）+ 可见密码框探测；判定优先级 URL 登录标记 → 密码框 → 验证码 → 中文关键词组 → 确认弹窗；`high` → `needs_human`/`human_prompt`/`intervention` 并入结果 data，`medium` 只记 `intervention` + 日志。检测绝不改变动作结果（status/content/error_type 原样保留），采样失败当"没发现"。
- **只读 execute_js**（P3-D1）：静态黑名单挡 fetch/eval/DOM 写/存储写/导航/表单点击/剪贴板写，结果是 best-effort，**不是沙箱**；真正的补口是动作后复查 URL。结果按 `MAX_JS_RESULT_CHARS=2000` 截断再进状态。
- **并发隔离**（P5，A2A 前置）：当前页放 `ContextVar`（`_page_var`）——**每个 asyncio 上下文/任务各一张页**，别的上下文拿不走；`_page_lock` 收窄到"取页的检查-动作对"（`_acquire_page`），页面动作不再全局串行；`_held_pages` 跨上下文记录借出的页，`aclose()` 全部归还（评测 harness 逐场景调用；**未接 app lifespan**）。残余：上下文结束后页不会自动归还（长跑有页 churn）；归还的页仍停在最后打开的 URL 上。

### 4.7 评测侧的装配（与 app 同一组合根）

`eval/harness.build_real_deps` 用**同一个** `Settings` 显式构造 LiteLLMProvider + Tier1/2 服务 + `AppToolExecutor` + `BrowserPool` + `NavigationPolicy`（不传的话 executor 会自己再读一次环境并静默覆盖，I3）；`--mock` 用 `FakeProvider`/`FakeExecutor`（无网络/无 key/无浏览器）。评测口径与数字见 `docs/v2-eval.md`；性能读数见 `docs/v2-performance.md`。

## 5. 关键决策 D1–D8 与分层红线

下表的内容/理由两列**逐字引自 spec §3.2**（不可回退，除非有充分理由）：

| 决策 | 内容 | 理由（spec 原文） |
|------|------|-------------------|
| D1 | **保留 LangGraph**（企业级框架） | checkpoint/HITL 中断恢复已跑通；手写循环风险大不可控；解耦与"用什么图库"正交 |
| D2 | **模型层 = LiteLLM 底座 + 自有薄封装** | 不重复造轮子（社区已有 100+ 厂商适配）；薄封装定义自有统一 Message/Usage/Cost 模型，这是分层的关键 |
| D3 | **可观测性 = Tracer 端口 + JSONL 默认 sink** | 现阶段全力保障 agent 执行效果，贪多嚼不烂；可观测性为未来的 agent 评估服务（文章 5） |
| D4 | **任务分层 Tier1/2/3 混合策略** | 简单信息任务不碰浏览器（可靠、便宜、快）；浏览器交互只留给真正需要的任务 |
| D5 | **前端保持 Vue3 只做必要修补** | 产品形态是 Agent 后端服务，前端只是演示层；用户本职工作为后端开发 |
| D6 | **A2A 端口**：V2 预留接口设计，实现放后期 | 架构上设计为"端口"，其他 agent 可通过标准协议调用 |
| D7 | **域名白名单改为可配置安全策略，默认不拦截合法 https** | 原白名单是 P0 任务失败的根因之一 |
| D8 | **版本策略**：原仓库新分支 `v2.0` | 类似大版本升级，保留历史 |

**实现对照**：D1 = `StateGraph` + `AsyncSqliteSaver`；D2 = `LiteLLMProvider` + 自有 `Message/Usage/Cost`；D3 = `Tracer`/`JsonlTracer`（Langfuse/LangSmith 未接）；D4 = `_STRATEGY_CHAIN` + `router`；D5 = `frontend-vue/` 未重建；D6 = **未实现**（§6）；D7 = `NavigationPolicy("allow_https")`；D8 = 分支 `v2.0`（`browsepilot.__version__ = "2.0.0"`；`pyproject.toml` 的 `version` 已于 P5-T5 收敛为 `2.0.0`，见 `docs/v2-release.md`）。

### 5.1 分层红线（spec §7.2 第 3 条）

> 分层单向依赖是硬性约束，禁止跨层 import（spec 原文）。

`tests/test_architecture.py` 用 AST 解析 `src/browsepilot/` 下每个文件的 import 做**静态**守卫
（不执行被测模块），当前共 **3 条**（v1 的 `browser_mcp` 守卫已在 P5-T1 随旧代码删除）：

| 守卫 | 断言 |
|------|------|
| `test_model_layer_never_imports_upper_layers` | `model/` 不 import `browsepilot.agent` / `browsepilot.app` |
| `test_agent_layer_never_imports_app_or_browser` | `agent/` 不 import `browsepilot.app`，也**不直接 import playwright**（浏览器驱动是 app.browser 的内部细节） |
| `test_app_browser_layer_is_self_contained` | `app/browser/` 不 import `browsepilot.agent`，且 app 层内只允许 `browsepilot.app.browser.*`（配置以裸参数传入） |

三条守卫都建立在"扫描到的模块清单"上，所以 `_layer_files()` 另带**扫描覆盖度前置断言**（model 5 / agent 9 / app.browser 6 个模块；包内总数 32）：目录改名/误删会让 `for path in []` **真空通过**，计数不符即失败（模块增减时需同步改期望值）。

`eval/` 不在这三条守卫里；另有一条同类约定：`src/` 不反向依赖 `eval/`（实测 `grep -rn "import eval\|from eval" src/ --include="*.py"` = 0 命中）。

## 6. 未实现 / 未验证清单（诚实面）

| 项 | 状态 | 证据 |
|----|------|------|
| **A2A 端口**（spec D6） | **未实现**：`src/browsepilot/app/` 无 a2a 模块、无路由挂载、`git log --all` 无 a2a 提交；P5-T2 深度待决策 | 本文档 §1 图注；spec §9 开放问题 |
| **Tier3 交互链**（click/type 入链） | 未编排：`browser_click`/`browser_type` 已是可调用工具，但 `_STRATEGY_CHAIN["browser_task"]` 只有 `[browser_open, browser_snapshot]`；对应场景 `t3_browser_interaction` = `enabled: false` | `graph.py`；`docs/v2-eval.md` §8.4 |
| **多轮架构缺口** | 已声明、未触发：全败模板路径不注入 history；`classify`/参数提取只看任务原文 | `graph.answer`；`docs/v2-eval.md` §8.5 |
| **Tracer 分片** | 进程级单 tracer（一进程一 trace_id/一个 JSONL 文件）；按会话/任务分文件未实现 | `app/main.py::_default_app`；§4.5 |
| **`aclose()` 未接 lifespan** | 评测 harness 逐场景调用；app 侧无归还点（长跑页 churn） | `executor.aclose()` docstring |
| **4 个 Settings 字段未接线** | `session_timeout_seconds` / `max_steps` / `max_replans` / `consecutive_failures_threshold` 仅在 `config.py` 定义（一处默认值断言）；当前图不读 | `grep -rn` 全仓（除 config.py 外仅 `tests/test_app/test_main.py` 断言默认值） |
| **`skills/` 目录** | v1 遗留，v2 代码零引用（`grep -rn "skills" src/ eval/ tests/ --include="*.py"` = 0 命中）；去留待决策 | P5 计划「删除范围」 |
| **HITL 端到端链路** | **未验证**（本机到 github.com/login 30s 超时，登录墙检测不可能触发）；`needs_human` 既不是通过也不是已确认缺陷 | `docs/v2-eval.md` §8.2 |
| **cost_usd / 并发 HTTP 读数** | 未采集（LiteLLM 价格表超时；`/health` 只有 TestClient 口径） | `docs/v2-eval.md` §8.1；`docs/v2-performance.md` §5 |

### 6.1 P5 已关闭（曾列于 §6 主表，收尾时解决）

| 项 | 处置 | 证据 |
|----|------|------|
| **版本号不一致** | **已于 P5-T5 收敛**：`pyproject.toml` 的 `version`、`uv.lock`、`browsepilot.__version__` 三处统一为 `2.0.0`（`/health` 报 `2.0.0`） | `docs/v2-release.md` §一（发布验收证据）/§八（提交）；本文件 §5 D8 对照组 |

> §6 主表写于 P5-T4（HEAD `7304bee`），是当时的实测状态；本小节记录其后的收敛结果。

---

*文档结束。口径更新请同步 `docs/v2-eval.md`（评测）与 `docs/v2-deploy.md`（部署）的交叉引用。*
