# 文章 5 素材：《Agent 的"黑匣子"：可观测性系统的设计与实现》

> 采集时间：2026-09-02（P2 Task 9 交付后，v2.0 分支）
> 文章主题：agent 运行时到底发生了什么——事件模型、span 语义、装饰器式模型观测、可插拔 sink
> 数据说明：真实 trace 语料 `data/traces/*.jsonl` 共 38 个文件（2026-08-16 ~ 2026-09-02，eval mock 模式与 P1 真实验收运行）；今日为素材补采 3 个受控样例（`demo-a/b/c`，stub 依赖、零 token、2026-09-02 14:04 UTC），保留在语料目录可直接查看

## 主线：事件协议先行，sink 可插拔

V1 没有 trace。V1 的可观测性 = 散落的日志字符串（"`[execute_node] Classifier selected: ...`"，见文章 3 素材阶段一引述），不可聚合、不可回放、不可关联会话。V2 从重构第一天（2026-08-03，commit `a51d011`）就引入了一个与业务无关的横切层：

- **端口**：`Tracer` Protocol——agent 核心只认识三个动作：`emit(事件)`、`span(节点)`、`new_span_id()`（`src/browsepilot/agent/tracer.py`）
- **模型**：`TraceEvent`——一条"发生了什么"的结构化事实，字段全集固定，与去向无关
- **默认 sink**：`JsonlTracer`——事件 → 一行 JSON，追加进 `data/traces/<trace_id>.jsonl`

模块 docstring 自己写明了设计意图（tracer.py 原文）：

> The agent core only knows "record events". Where they go (file, Langfuse,
> LangSmith) is a pluggable sink. V2 default: JSONL files.

Langfuse/LangSmith 至今一个都没实现——**这正是设计要证明的事**：换观测后端 = 写一个实现 `Tracer` 协议的新类，事件模型与全图埋点一行不用动。写文章时这是一个"端口比实现先到"的叙事：可观测性是一等公民，不是事后补丁。

## TraceEvent：字段全集（格式无关的"种子"）

`TraceEvent`（tracer.py）是一个普通 Pydantic 模型，字段全集如下：

| 字段 | 类型 | 含义 | 谁填 / 何时非空 |
|------|------|------|----------------|
| `trace_id` | str | 一次 trace 的归属 id（= tracer 实例 id，见"粒度"节） | 所有事件 |
| `span_id` | str | 事件归属的 span id | 所有事件 |
| `parent_span_id` | str \| None | 父 span id（嵌套时非空） | 仅嵌套发生时（见修复故事） |
| `node` | str | 图节点名：`classify`/`execute`/`answer`/`hitl`/`llm` | 所有事件 |
| `event_type` | str | `node_start` \| `node_end` \| `tool_call` \| `llm_call` \| `decision` \| `error` \| `info` | 所有事件 |
| `timestamp` | str | UTC ISO 8601（`datetime.now(UTC).isoformat()`） | 所有事件 |
| `data` | dict[str, Any] | 开放式事件载荷 | 通常非空 |
| `duration_ms` | float \| None | 耗时（毫秒） | 仅 `node_end` 与 `llm_call` |

五个设计点（都可作为文章论点）：

1. **事件流是"事实"不是"树"**：落盘格式是扁平的 append-only 事件流，每行一个不可变事实；span 的父子树由 `parent_span_id` 事后重建，而不是在写入时维护树结构。这让 sink 极简单（追加一行），也让"事后聚合"（按 trace_id 过滤、按 span_id 拼接起止）成为消费者的事。
2. **格式无关**：`TraceEvent` 是唯一契约。今天的编码只是 `json.dumps(event.model_dump(), ensure_ascii=False)` 追加一行；未来任何 sink（Langfuse/LangSmith/OpenTelemetry/msgpack）都消费同一个模型。这就是"pi-insights 种子"——一个以后可以做洞察分析的统一事件模型。
3. **`data` 是开放的 dict**：事件类型固定，细节靠 `data` 演进。实例：`llm_call.data` 的 `kind`/`model`/`session_id` 是 T6 设计期就定下的（commit `858e77b`）；评审还讨论过把 token usage 摘要放进 `data`（deferred 未做）——将来加字段不破坏旧消费端，就是开放 dict 的红利。对比另一面：`event_type` 只是带注释的裸 `str`（注释里枚举了 7 种取值），不是 `Literal` 枚举——与文章 2/3 里"模型输出用 Literal 掐死"的哲学形成有趣反差：对模型输出我们绝不允许未知值，对自家埋点我们却容忍自由字符串。可以讨论：`event_type` 要不要也换成 `Literal`（成本：每加一种事件类型要改模型；收益：拼写错误编译期暴露）。
4. **`duration_ms` 可为 null**：只有"结束"类事件（`node_end`、`llm_call`）填耗时，"开始"类与瞬时事件（`decision`、`tool_call`）是 null。消费端必须容忍 null——这是 schema 里 `float | None` 明说的。
5. **`timestamp` 全部 UTC ISO**：不做本地时区，日志分析跨机器可比。

## 事件类型全集：谁在什么时机发什么

| 事件类型 | node | data 内容 | 生产者（代码位置） | 时机 |
|---------|------|----------|------------------|------|
| `node_start` | classify | `{"task": task[:60]}`（截断到 60 字符） | `graph.py::classify` 的 `with tracer.span("classify", task=...)` | 节点进入 |
| `node_start` | execute | `{"intent": ...}` | `graph.py::execute` 的 `with tracer.span("execute", intent=...)` | 节点进入 |
| `node_start` | answer / hitl | `{}`（无载荷） | `graph.py::answer` / `graph.py::hitl` 的 `tracer.span(...)` | 节点进入 |
| `node_end` | 同上 | `{}` | 同一 span 对象退出（`tracer.py::TraceSpan.__exit__`） | 节点退出，填 `duration_ms` |
| `decision` | classify | `RouteDecision.model_dump()` → `{intent, tier, confidence}` | `graph.py::classify` 末尾 `tracer.emit(...)` | 路由决策产生（规则或模型路径都发） |
| `tool_call` | execute | `{"tool": ..., "status": "success"\|"error"}` | `graph.py::execute` 策略链循环内，每次尝试后 `tracer.emit(...)` | 每次工具尝试（含失败） |
| `llm_call` | llm | `{"kind": "chat"\|"stream"\|"structured", "model": ..., "session_id": ...}` | `tracer_provider.py::TracingModelProvider._emit` | 每次 LLM 调用完成，填 `duration_ms` |
| `error` | — | — | **暂无生产者**（模型预留） | — |
| `info` | — | — | 暂无生产者（仅测试手工构造过） | — |

要点：

- **降级链可追溯**（文章 3 主题的观测面）：`execute` 的策略链循环每尝试一个策略就发一条 `tool_call`，失败与成功一样留痕。一次"天气城市未命中 → 用任务原文搜索兑底"的降级，在 trace 里就是同一个 `execute` span 内两条 `tool_call`（`weather` error → `search` success）。触发实验见"样例 demo-a"。
- **`tool_call` 只带 `status`，不带失败原因**：`error_type`/`error_message`（如 `city_not_found`）只进 `AgentState`（degradation_log / result_data）与回答文案，不进事件。trace 记"发生了什么"，状态记"为什么"，用户看到"怎么解释"——三层分工，可作文章讨论点（要不要给 `tool_call.data` 补 `error_type`，或引入真正的 `error` 事件）。
- **`llm_call` 是唯一"非节点"事件**：node=`llm`，独立 span_id、无 `node_end` 配对、无父 span（见"开放问题 1"）。
- **classify 规则命中时 span 内没有 `llm_call`**——文章 3 的"规则优先省调用"在观测数据里直接可见（语料 167 次决策全部规则命中、confidence 1.0，见数据节）。
- 全图四个节点 classify/execute/answer/hitl 都有 span 包着，但 **hitl 在既有语料里零样本**（见数据节缺口）。

## span 语义修复故事（真实 bug，commit `0a20ea2`）

### Bug 形态：start/end 各拿一个新鲜 span_id

Tracer 诞生（`a51d011`，2026-08-03）时 `TraceEvent` 就有 `parent_span_id` 字段，但**从第一天起没有任何生产者给它赋值**——一个空转的设计字段。更糟的是 `TraceSpan` 的实现让 span 根本拼不起来（`git show 0a20ea2^` 的原文 + 注解，`# ←` 注释为注解）：

```python
def __enter__(self) -> "TraceSpan":
    self._tracer.emit(
        TraceEvent(
            trace_id=self._tracer.trace_id,
            span_id=self._tracer.new_span_id(),   # ← 每次进入现造一个
            node=self._node,
            event_type="node_start",
            data=self._data,
        )
    )
    return self

def __exit__(self, exc_type, exc, tb) -> None:
    self._tracer.emit(
        TraceEvent(
            trace_id=self._tracer.trace_id,
            span_id=self._tracer.new_span_id(),   # ← 退出又现造一个
            node=self._node,
            event_type="node_end",
            duration_ms=(time.monotonic() - self._start) * 1000,
        )
    )
```

后果：一条"span"在盘上是**两条无法关联的事件**——`node_start` 的 span_id 是 s1，`node_end` 的 span_id 是 s2，没有任何键能把它们拼成一个带耗时的节点执行；`parent_span_id` 字段形同虚设。也就是说：文件能落、事件能数，但"这次 classify 花了多久""execute 里那次 llm 调用属于哪个节点"这类最基本的问题答不上来。黑匣子录了音，但每句话都丢了说话人标签。

（注：现存语料最早记录是 2026-08-16——修复（08-13）之后。bug 形态没有污染过盘上数据，取证只能靠 git：`git show 0a20ea2^:browsepilot/src/browsepilot/agent/tracer.py` 看坏代码，`git show 0a20ea2` 看修复全貌。写文章时这是个"代码评审在坏数据产生前拦下语义缺陷"的素材——也可以诚实说：当时根本没有消费 trace 的工具，所以这个 bug 是"读代码"读出来的，不是"读数据"读出来的。）

### 修复：一个 span 一个 id + contextvar 栈做父关联（`0a20ea2`，2026-08-13）

修复后的语义（tracer.py 模块 docstring 原文）：

> Span semantics: one span owns ONE span_id, shared by its node_start and
> node_end events. Nesting is tracked per-tracer via a contextvar stack
> (task-local, async-safe), so concurrent graph runs on the same tracer
> instance don't corrupt parent linkage.

机制拆解（全部在 tracer.py）：

- **id 在构造时铸造一次**：`TraceSpan.__init__` 里 `self._span_id = tracer.new_span_id()`，`__enter__`/`__exit__` 都发这一个 id → 一条 span = 一对可拼接的 start/end 事件。
- **父 id 在进入前取栈顶**：`JsonlTracer.span()` 读 `self._span_stack.get()`，栈顶即父 span，没有则 `None`——所以顶层 span 的 `parent_span_id` 是 null，嵌套 span 自动拿到父 id。
- **进出栈用 contextvar token**：`__enter__` 里 `self._stack_token = self._stack.set(current + (self._span_id,))`，`__exit__` 里 `self._stack.reset(self._stack_token)`。用 token 复位而不是"弹栈"，异常路径也安全（`__exit__` 无论成败都会执行）。
- **栈是 task-local 的**：`JsonlTracer.__init__` 里 `self._span_stack: ContextVar[tuple[str, ...]] = ContextVar(f"bp_span_stack_{id(self)}", default=())`。名字带实例 id，多 tracer 实例互不串栈。

为什么非得 contextvar 而不是实例属性 `self._current_span`？因为生产组装里 **JsonlTracer 是进程级的单例**（`src/browsepilot/app/main.py::_default_app` 每个进程一个），而 FastAPI 是并发处理请求的——两个用户的任务可以交错执行。若用实例属性记"当前 span"，请求 A 的 classify 会把请求 B 的 execute 认成自己的子节点；contextvar 让每个 asyncio 任务看到自己的栈快照，互不污染。这是"并发安全不是加分项而是必须项"的典型例子：单测里顺序跑看不出来，上服务器并发才炸。

修复同 commit 补的测试（`tests/test_agent/test_tracer.py`）把语义钉死：

- `test_span_start_and_end_share_span_id`——起止同 id、顶层父为 null
- `test_nested_spans_link_parent_ids`——嵌套时内层 start 的父 = 外层 span_id，且内层起止同 id
- `test_sibling_spans_have_no_parent_after_exit`——兄弟 span 出栈后父还原为 null

### 诚实注脚：真实语料里 parent_span_id 全是 null

图是顺序执行的：classify 结束才进 execute，任何时刻栈深 ≤ 1，所以 38 个语料文件里**没有一条非空 parent_span_id**——栈机制目前只被单元测试的嵌套场景真正压过。它是"为并发与嵌套准备好的能力"，而真正需要它的时刻是 `llm_call` 嵌套（见开放问题 1）与将来可能出现的并行工具调用。写文章时别把它吹成"已经用上的嵌套追踪"，正确的说法是：**语义先修对，消费端后到**。

## TracingModelProvider：装饰器模式给模型层加观测（零侵入）

LLM 调用是 agent 的成本与延迟大头，观测它最值钱。但 V1 式的做法是在每个调用点手写日志——V2 的做法是**装饰器**（`src/browsepilot/agent/tracer_provider.py`）：

```python
class TracingModelProvider:
    """Drop-in ModelProvider wrapper: delegates calls, emits llm_call events."""

    def __init__(self, inner: ModelProvider, tracer: Tracer, session_id: str = ""):
        self._inner = inner
        self._tracer = tracer
        self._session_id = session_id
```

三个方法 `chat` / `stream` / `structured` 同构：`time.monotonic()` 起表 → 委托 `inner` → 完成后 `_emit("chat"|"stream"|"structured", model, start)`。`_emit` 发一条 `llm_call` 事件（node=`llm`），data 带 `kind`/`model`/`session_id`，`duration_ms` 填单调钟耗时。

包装发生在装配处（`src/browsepilot/agent/api.py::run_task` / `api.py::resume_task`）：构图时把 provider 包一层再传进 `build_graph`。效果：

- **图代码零改动**：classify 的意图分类、execute 的参数提取、answer 的回答合成——每一处 LLM 调用自动留痕，无需在 graph.py 加任何埋点。
- **想拆掉观测 = 少包一层**：装饰器嵌套是纯加法，A/B 对比"带观测 vs 不带"的延迟/成本影响很容易做。

边界事实（写作时可用，全是评审记录在案的 deferred 项）：

1. **inner 抛异常时不发任何事件**——`_emit` 只在成功后执行，`llm_call` 没有 error 通路（T6 报告原文：error-trace coverage would be a later task）。LLM 失败在 trace 里是"缺席"，不是"错误事件"。
2. **token usage 未入 data**：T6 评审 deferred（usage 摘要未入 data）。
3. **文档字符串写 wall-clock，实现是 monotonic**（T7 记录措辞偏差）——无关紧要，但写文章引用时用词要准。
4. `stream` 的语义是**耗尽后**才发事件（发生器 drain 完），中途断流/客户端放弃则事件缺失——与 SSE 场景的"半截流"问题同源（见开放问题 3）。

## JSONL sink 与可插拔：为什么默认是"最土的"方案

`JsonlTracer`（tracer.py）的全部落盘逻辑：

```python
def emit(self, event: TraceEvent) -> None:
    line = json.dumps(event.model_dump(), ensure_ascii=False)
    with self._lock:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
```

- 一个 trace_id 一个文件：`data/traces/<trace_id>.jsonl`；线程锁保证并发请求下**行不被写碎**（一行一次 open+append，进程内安全；多 worker 各写各的文件，天然无跨进程竞争）。
- 选 JSONL 的理由（文章素材）：人可读、`grep` 可查、`tail -f` 可盯、不需要部署任何 agent/daemon/数据库。对一个"先证明事件模型对不对"的阶段，土方案的信息密度最高。
- 中文原样落盘（`ensure_ascii=False`），`grep 北京 data/traces/*.jsonl` 直接可用。

### msgpack 前向兼容注记（T1 实证，非本文代码）

可观测性的另一面是把运行事实持久化——checkpoint。P2 Task 1（T1 报告）在直跑探针里观察到 langgraph 的警告：

> Deserializing unregistered type browsepilot.agent.state.AgentState from checkpoint. This will be blocked in a future version...

即：未来的 strict-msgpack（`LANGGRAPH_STRICT_MSGPACK`）会拒绝未注册的自定义类型，需要显式注册 `allowed_msgpack_modules=["browsepilot.agent.state"]`，注册点应在 saver 构造处（`src/browsepilot/app/main.py::lifespan` 的 `AsyncSqliteSaver.from_conn_string(...)`）。截至本素材采集，该注册**尚未落地**（源码中无 `allowed_msgpack_modules`）——这是"模型层契约稳定、序列化格式演进"主题的真实案例，与 trace 事件模型的"格式无关"是同一枚硬币的两面：TraceEvent 用纯 dict + JSON 保住可迁移性，checkpoint 用 pydantic 模型 + msgpack 但欠着一笔格式升级的债。

## session_id 关联链与 trace_id 粒度（诚实观察）

**关联链（代码路径）**：HTTP 层推导 `effective_session_id`（`src/browsepilot/app/main.py::/chat/stream`：`req.session_id or str(uuid4())`）→ `src/browsepilot/agent/api.py::run_task(session_id=...)` → 同时喂给 `AgentState(session_id=...)`（进图、随 checkpoint 持久化、当 thread_id 用）和 `TracingModelProvider(..., session_id)`（进 `llm_call.data.session_id`）。所以 trace 里能按 `data.session_id` 把一个会话的所有 LLM 调用捞出来——**但只有 llm_call 事件带会话号**，node/tool 事件不带。

**trace_id 粒度 = tracer 生命周期**（写文章最值得讲透的一点）：

| 场景 | tracer 怎么来的 | 一个 trace 文件装什么 |
|------|----------------|---------------------|
| 服务器 | `_default_app` 每进程一个 `JsonlTracer` | **该进程所有会话、所有任务**（进程级粒度） |
| eval | `eval/run_eval.py::main` 每次运行一个 | 3 个 P0 场景任务（整轮评测） |
| 单元测试 | 每测试一个（tmp 目录） | 单任务 |

观察：eval 的"一个文件 3 个任务"靠 classify `node_start.data.task` 区分任务边界；服务器的"进程级文件"连这个边界都没有（只能靠时间戳与 llm_call 的 session_id 猜）。按任务/会话切 trace_id、父子 trace（run → task → node）是明显的演进点——`data/traces/6a80384b-*.jsonl` 就是一次 eval 装 3 个任务的实例，可直接引用。

## 真实数据

### 语料总览（既有 38 个文件，不含今日 demo 文件）

- 时间范围：2026-08-16T15:07Z ~ 2026-09-02T13:56Z；1174 条事件
- 分布：`node_start` 435 / `node_end` 435 / `decision` 167 / `tool_call` 101 / `llm_call` 36（chat 18、structured 18；stream 0）
- 决策全貌：167 次全部规则命中——weather/1.0 ×101、search/1.0 ×33、read_page/1.0 ×33；**无 0.8（模型回退）实例**（语料是评测场景，设计上规则可命中，选择偏差要诚实）
- 工具调用 101 次全部 `status=success`；**无失败/降级/hitl/error 样本**；`session_id` 全部为空串；非空 `parent_span_id` 0 条
- 数据缺口即文章素材：**既有语料只能讲"顺利的一天"，降级链、模型回退、HITL 的形态要靠今日 demo 补**

### 既有语料实样（文件头 3 行，`data/traces/6a80384b-6b23-4629-9a1a-5706ed40a35c.jsonl`，2026-09-02 13:56，eval mock）

```json
{"trace_id": "6a80384b-6b23-4629-9a1a-5706ed40a35c", "span_id": "1fc7f635", "parent_span_id": null, "node": "classify", "event_type": "node_start", "timestamp": "2026-09-02T13:56:06.460947+00:00", "data": {"task": "打开 https://www.python.org 告诉我页面主要内容"}, "duration_ms": null}
{"trace_id": "6a80384b-6b23-4629-9a1a-5706ed40a35c", "span_id": "d8475da8", "parent_span_id": null, "node": "classify", "event_type": "decision", "timestamp": "2026-09-02T13:56:06.461321+00:00", "data": {"intent": "read_page", "tier": "tier2", "confidence": 1.0}, "duration_ms": null}
{"trace_id": "6a80384b-6b23-4629-9a1a-5706ed40a35c", "span_id": "1fc7f635", "parent_span_id": null, "node": "classify", "event_type": "node_end", "timestamp": "2026-09-02T13:56:06.461651+00:00", "data": {}, "duration_ms": 0.702600000295206}
```

读法示范（写文章时的"如何看一条 trace"教学素材）：第 1/3 行 span_id 相同（`1fc7f635`）= 一个完整 span，起 0ms 止 0.7ms；第 2 行是夹在中间的瞬时事实——规则直接命中 read_page，`confidence: 1.0`，**span 内没有 llm_call，证明这次分类没花钱**。

### 补采样例 demo-a：一次完整的降级链（`data/traces/demo-a-weather-city-miss.jsonl`）

受控生成（stub：天气工具返回 `city_not_found`，搜索兜底成功；任务"北京今天天气怎么样？"，session_id=`demo-session-a`）。11 条事件逐行解读（t0 = 2026-09-02T14:04:27.503677Z）：

| # | t+ms | 事件 | span_id | 解读 |
|---|------|------|---------|------|
| 0 | 0.00 | node_start classify | `7268ab4d` | 任务进入，data 带 task |
| 1 | 0.61 | decision | `be420782` | 规则命中 weather/tier1/1.0（无 LLM） |
| 2 | 0.84 | node_end classify | `7268ab4d` | 与 #0 同 id，duration 0.84ms |
| 3 | 2.19 | node_start execute | `2f91a665` | data 带 intent=weather |
| 4 | 2.45 | llm_call | `88250a27` | structured 提取城市参数（session_id 已入 data） |
| 5 | 2.65 | tool_call weather | `3ff911f6` | **status=error**——城市未命中 |
| 6 | 2.83 | tool_call search | `059e1ba4` | **兑底策略成功**；注意中间没有第二次 llm_call（代码路径：降级到 search 时直接用任务原文当 query，见 graph.py::execute） |
| 7 | 2.98 | node_end execute | `2f91a665` | duration 0.79ms，内含两次工具尝试 |
| 8 | 3.73 | node_start answer | `74989541` | — |
| 9 | 3.96 | llm_call | `1d1ef507` | chat 合成回答 |
| 10 | 4.19 | node_end answer | `74989541` | duration 0.45ms |

原文一行实样（第 5 条）：

```json
{"trace_id": "demo-a-weather-city-miss", "span_id": "3ff911f6", "parent_span_id": null, "node": "execute", "event_type": "tool_call", "timestamp": "2026-09-02T14:04:27.506325+00:00", "data": {"tool": "weather", "status": "error"}, "duration_ms": null}
```

这是既有语料缺、而文章最需要的形态：**同一条 execute span 里两条 tool_call（error → success）= 降级链的可视化签名**。读者一眼能看出"第一次尝试失败、第二次换策略成功"。

### demo-b：全策略失败（`demo-a` 的对照，`data/traces/demo-b-search-all-fail.jsonl`）

search 单策略意图，工具失败 → 链耗尽。9 条事件，关键行：`tool_call search status=error` 后 execute 直接结束，**answer span 里没有任何 llm_call**——因为降级回答文案是纯拼接的（graph.py::answer 里 `all_strategies_failed` 分支把 degradation 逐条拼进"抱歉，这个任务暂时无法完成：……"）。故事点：**trace 里 llm_call 的缺席本身就是"这次失败回答零 token"的证据**——黑匣子不仅能证明花了钱，还能证明没花钱。

### demo-c：规则未命中走模型（`data/traces/demo-c-rule-miss-model-fallback.jsonl`）

任务"帮我处理一下那个事情"（规则外表达）→ classify span 内出现 `llm_call structured`（分类调用），随后 decision 的 confidence 是 **0.8**（模型路径，graph.py::classify 赋值）——与规则路径的 1.0 在 trace 里可直接区分。文章 3 的"规则→模型→兜底"三档在这里有了观测证据链：classify span 内有无 llm_call + decision.confidence 两个信号就能判定走了哪档。

### 黑匣子的边界（诚实素材）

trace 文件里**没有**：最终回答文本（final_answer 在 SSE `answer` 事件与 turns 表里）、degradation 明细（在 AgentState/result_data）、HITL 的人工回复。还原一次完整交互要拼 trace + SSE + 会话存储三处。这本身是文章的好问题：**黑匣子该录到哪一层？** 事件层（trace）、状态层（state/checkpoint）、用户可见层（SSE/会话）三层各记什么，现在的边界是否是好的边界？

## 采集与复现方法（给作者）

```bash
cd D:/AI_Agent_Demo/browsepilot

# eval mock 模式（离线、0 token）：跑完 data/traces/ 多一个文件，内含 3 个 P0 场景任务
uv run python -m eval.run_eval --mock

# 真实模式（需 BP_MODEL_API_KEY 等 env）：同命令不带 --mock
uv run python -m eval.run_eval

# 服务器模式：进程级单 tracer —— 每请求追加进同一个文件
uv run uvicorn browsepilot.app.main:app --port 8001
# 另开终端 curl 触发任务（SSE）：POST /chat/stream {"task": "...", "session_id": "..."}

# demo 文件的生成方式：stub 版 DemoProvider/DemoExecutor（结构化返回固定 schema、工具按需失败）
# + 真实 run_task + JsonlTracer(trace_id="demo-...")，脚本未入库（一次性），文件已保留可查
```

`data/traces/` 整体 gitignored，不随仓库提交。

## 开放问题（供文章讨论，均有出处）

1. **llm_call 的 flat event vs span 嵌套**（T6 评审 deferred，product-owner 待确认——本素材最重要的讨论题）：现状 llm_call 是"事件"不是"span"——无父、无 node_end 配对。但它发生在某个节点 span 内部（classify/execute/answer 的 with 块里），而 contextvar 栈明明能给出当时的父 span。三个可选方案：
   - 保持 flat，但 emit 时读栈顶把 `parent_span_id` 补上（事件化关联，零生命周期成本）——代价：仍然没有起止配对，耗时只有总耗时没有分段；
   - 改成 `tracer.span("llm")` 包裹——自动获得父链与 start/end，但每次 LLM 调用多两条事件，且失败路径（inner 抛异常）下 span 语义要重新定义；
   - 维持现状，把"一次调用一条事件"当查询友好的最小事实。
   评审原文可引用："llm_call 事件扁平无 parent 嵌套（span vs 事件取舍——文章 5 素材点，product-owner 确认）"。
2. **trace_id 粒度**：进程级 tracer 意味着 trace 边界不是任务边界（见上表）。演进方向：per-task tracer + 父 trace 引用，或事件加 request_id/run_id 字段。
3. **失败在 trace 里是"缺席"而不是"事件"**：LLM 异常无 llm_call、流中断无事件、工具失败只有 status 无原因。要不要 error 事件类型真正投产？
4. **hitl span 重开两次**（T4 评审 deferred）：resume 时 LangGraph 重放 hitl 节点 → 同一次人工确认在 trace 里出现两对 hitl node_start/node_end（interrupt 暂停一次、resume 完成一次）。中断语义目前只能靠时序与 SSE 事件推断，trace 自身不带"interrupt"标记。
5. **8 字符 span_id 的碰撞空间**（评审 deferred 注记）：`uuid4()[:8]` ≈ 32bit。低流量可接受；语料变大或做跨系统关联时需加长或换 16 字符。
6. **`final_answer` 不在 trace 里**（见"黑匣子的边界"）——事件层与状态层的分工是否合理。

## 代码位置索引（供引用，路径 + 符号，无行号）

| 内容 | 位置 |
|------|------|
| TraceEvent 模型 / TraceSpan / JsonlTracer / Tracer Protocol | `src/browsepilot/agent/tracer.py` |
| llm_call 装饰器（chat/stream/structured/_emit） | `src/browsepilot/agent/tracer_provider.py::TracingModelProvider` |
| 装饰器装配点（包 provider） | `src/browsepilot/agent/api.py::run_task`、`api.py::resume_task` |
| classify span + decision 事件 | `src/browsepilot/agent/graph.py::classify` |
| execute span + 每尝试一条 tool_call（降级链） | `src/browsepilot/agent/graph.py::execute` |
| answer / hitl span | `src/browsepilot/agent/graph.py::answer`、`graph.py::hitl` |
| 降级文案（all_strategies_failed 分支，零 LLM） | `graph.py::answer` |
| span 修复 commit（bug 形态与修复对照） | `git show 0a20ea2`；坏代码 `git show 0a20ea2^:browsepilot/src/browsepilot/agent/tracer.py` |
| Tracer 引入 commit | `a51d011` |
| span 语义测试 | `tests/test_agent/test_tracer.py`（share_span_id / nested / sibling 三个用例） |
| llm_call 测试 | `tests/test_agent/test_tracer_provider.py` |
| 图事件形状断言（decision/tool_call 的 data 契约） | `tests/test_agent/test_graph.py`（含 chitchat 无 tool_call 断言） |
| 进程级 tracer 组装 / trace_dir 配置 | `src/browsepilot/app/main.py::_default_app`；`src/browsepilot/app/config.py::Settings.trace_dir`（默认 `data/traces`） |
| session_id 推导与传入 | `src/browsepilot/app/main.py::/chat/stream` → `api.py::run_task` |
| eval 采集（每次一个 tracer、3 场景一文件） | `eval/run_eval.py::main`、`run_eval.py::run_all` |
| 场景定义 | `eval/scenarios/p0_weather.yaml`、`p0_search.yaml`、`p0_read_page.yaml` |
| checkpoint saver 构造（msgpack 注册点，未落地） | `src/browsepilot/app/main.py::lifespan`（`AsyncSqliteSaver.from_conn_string`） |
| 真实语料 / 本次 demo 文件 | `data/traces/*.jsonl`（gitignored）；`demo-a-weather-city-miss.jsonl`、`demo-b-search-all-fail.jsonl`、`demo-c-rule-miss-model-fallback.jsonl` |

## 一句话素材（供作者选用）

- "黑匣子录了音，但每句话都丢了说话人标签"——span_id bug 的一行版。
- "失败在 trace 里不是错误事件，而是缺席"——error 通路缺失的一行版。
- "llm_call 的缺席，就是这次降级回答零 token 的证据"——demo-b 的一行版。
- "trace 的边界不是任务，是 tracer 的生命周期"——粒度问题的一行版。
- "端口比实现先到：Langfuse/LangSmith 一行代码都没写，换 sink 的路径已经画好了。"
