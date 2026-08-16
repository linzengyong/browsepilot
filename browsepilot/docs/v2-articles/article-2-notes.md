# 文章 2 技术笔记：《LLM 结构化输出之战：如何让 AI 稳定返回 JSON》

> 联动任务：T5（model/structured.py + litellm_provider.structured），已实现并测试通过

## 三种方案对比

| 方案 | 机制 | 优点 | 缺点 |
|------|------|------|------|
| JSON Mode | 模型侧声明输出为 JSON | 简单、各厂商都支持 | 无 schema 约束：字段名、类型、嵌套结构全凭模型心情 |
| Function Calling（强制 tool_choice） | 用 function schema 约束输出结构 + `tool_choice` 强制走该 tool | schema 约束、类型提示、兼容嵌套 | 各厂商 tool calling 行为有差异；DeepSeek 的 json_object 模式不保证符合 schema |
| Pydantic 校验（最终防线） | 解析结果进 Pydantic 模型强校验 | 类型/必填/枚举约束兜底、失败可重试 | 校验失败仍需重试策略处理 |

## 我们的选择：三层组合拳

```
forced tool calling（tool_choice 锁定 emit_result）
  → JSON 解析（json.loads）
  → Pydantic 校验（schema(**data)）
  → 失败重试 max_retries 次
  → 全败抛 StructuredOutputError
```

- `tool_spec_for()` 把 Pydantic JSON Schema 包成 function tool（tool 名 `emit_result`）
- `schema_for()` 从 model 的 docstring 取 description（提示词的一部分）
- `structured()` 在 `litellm_provider.py`，重试期间记录每次失败原因（logger.warning）

## 代码位置

| 内容 | 位置 |
|------|------|
| schema_for / tool_spec_for / StructuredOutputError | `src/browsepilot/model/structured.py` |
| structured() 重试逻辑 | `src/browsepilot/model/litellm_provider.py::structured` |
| 测试（含类型错误重试） | `tests/test_model/test_structured.py` |

## 为什么不用纯 JSON Mode

1. 无法表达嵌套 schema / 枚举约束 / 必填字段
2. DeepSeek 的 json_object 模式只保证"是 JSON"，不保证"符合你的 schema"
3. tool calling 的 schema 本身就是给模型的强提示——字段名、类型、描述都会影响生成质量

## 踩过的坑（测试里固化的场景）

- LLM 可能返回 `{"city": 123, "temperature_c": "x"}`——类型全错。Pydantic 校验直接拒绝 → 重试（`test_structured_retries_then_raises`）
- LLM 可能返回非法 JSON（截断、多余逗号）——`json.loads` 抛错 → 重试
- 必须捕获 `ValidationError`，不能只捕获 `JSONDecodeError`（校验失败和解析失败是两层问题）

## 可演进点（留给后续文章/阶段）

- 把 Pydantic 校验错误信息回传给模型做"自我修复重试"（当前是盲重试）
- 记录每次重试的 tool_calls 原始输出到 Tracer，量化"一次成功率"

## 实战案例（P1）：参数提取 + 重试策略的落地

> 联动任务：T2（tenacity 重试）、T7（结构化参数提取）。前半部分讲"怎么让 AI 稳定返回 JSON"，这一节讲"稳定之后拿来干什么、不稳定时怎么兜"。

### 案例一：三个工具的参数提取（WeatherParams / SearchParams / ReadPageParams）

代码位置：

| 内容 | 位置 |
|------|------|
| 三个参数模型定义 | `src/browsepilot/agent/params.py`（WeatherParams / SearchParams / ReadPageParams） |
| 意图 → (工具, 参数模型) 映射表 | `src/browsepilot/agent/graph.py::execute` 的 `_TOOL_BY_INTENT` |
| 提取失败降级 | `graph.py::execute`（StructuredOutputError → `param_extraction_failed`）、`graph.py::answer`（明确告知原因） |

执行流程（graph.py::execute）：

```python
_TOOL_BY_INTENT: dict[str, tuple[str, type[BaseModel]]] = {
    "weather": ("weather", WeatherParams),
    "search": ("search", SearchParams),
    "read_page": ("fetch_page", ReadPageParams),
}
...
params = await provider.structured(
    [
        Message(role="system", content="从用户任务中提取参数。"),
        Message(role="user", content=state.task),
    ],
    schema=params_model,
    model=small_model,
)
tool_result = await executor.call(tool_name, params.model_dump())
```

文章可用的观点：

- **参数模型就是工具的"函数签名"**：`schema_for()` 从模型的 docstring 取 description（`city 为中文城市名，如 北京、上海`），docstring 直接成为提示词的一部分——写模型文档就是写提示词。
- **一次 structured 调用拿到全部参数**：不再像 v1 那样"规则选工具 → LLM 补参数"两段式（旧 backend/app/agent/nodes.py）。
- **提取失败不瞎猜**：StructuredOutputError 被捕获后转为 `param_extraction_failed` 错误结果，answer 节点原样转达（"抱歉，这个任务暂时无法完成：…"），绝不编造参数继续执行。
- 测试固化：`tests/test_agent/test_params.py`（模型可实例化）、`tests/test_agent/test_graph.py::test_weather_task_runs_tool_and_synthesizes`、`test_tool_failure_degrades_with_explicit_reason`。

### 案例二：手写重试 → tenacity（before / after）

代码位置：`src/browsepilot/model/litellm_provider.py::structured`（重构提交 `4fc5f2b refactor(model): replace hand-rolled retry with tenacity`）。

Before（手写循环，旧版本）：

```python
last_error: Exception | None = None
for attempt in range(max_retries):
    resp = await litellm.acompletion(
        model=model or self.model, messages=msgs, tools=[tool],
        tool_choice={"type": "function", "function": {"name": tool["function"]["name"]}},
        **self._kwargs(),
    )
    try:
        call = resp.choices[0].message.tool_calls[0].function
        data = json.loads(call.arguments)
        return schema(**data)
    except (IndexError, AttributeError, json.JSONDecodeError, ValidationError) as exc:
        last_error = exc
        logger.warning("structured attempt {} failed: {}", attempt + 1, exc)
raise StructuredOutputError(
    f"structured output failed after {max_retries} attempts: {last_error}"
)
```

After（tenacity，当前 v2.0）：

```python
@retry(
    stop=stop_after_attempt(max_retries),
    retry=retry_if_exception_type(
        (IndexError, AttributeError, json.JSONDecodeError, ValidationError)
    ),
    wait=wait_fixed(0),  # LLM 重试保持零等待（与 P0 行为一致）；后续可换 wait_exponential
)
async def _attempt() -> BaseModel:
    resp = await litellm.acompletion(
        model=model or self.model, messages=msgs, tools=[tool],
        tool_choice={"type": "function", "function": {"name": tool["function"]["name"]}},
        **self._kwargs(),
    )
    call = resp.choices[0].message.tool_calls[0].function
    data = json.loads(call.arguments)
    return schema(**data)

try:
    return await _attempt()
except RetryError as exc:
    last = exc.last_attempt.exception()
    logger.warning("structured output failed after {} attempts: {}", max_retries, last)
    raise StructuredOutputError(
        f"structured output failed after {max_retries} attempts: {last}"
    ) from last
```

对比表：

| 维度 | 手写 for 循环 | tenacity |
|------|--------------|----------|
| 重试条件 | 异常元组写死在 except | `retry_if_exception_type(...)` 声明式配置 |
| 成功路径 | 循环里提前 return | 函数体只管业务，无循环噪音 |
| 失败收尾 | 循环外 raise | 捕获 `RetryError`，`exc.last_attempt.exception()` 取最后异常 |
| 等待策略 | 无（立即重试） | `wait_fixed(0)` 保持 P0 行为，注释注明可换 `wait_exponential` |
| 日志 | 每轮手动 warning（含 attempt 序号） | 耗尽后统一 warning |
| 可读性 | 重试逻辑与业务交织 | 装饰器自解释：3 次、重试哪些异常、等多久 |

一个实现细节：`@retry` 装饰的是方法内的闭包函数 `_attempt` 而非方法本身——因为 `max_retries` 是每次调用的运行时参数，装饰器需要捕获当前调用的 `msgs`/`tool` 闭包。

### 案例三：重试策略是"跨切面"的关注点

P1 的教训："失败重试"不是某一个函数的事，三层都要，统一用 tenacity：

| 位置 | 重试对象 | 策略 |
|------|---------|------|
| `model/litellm_provider.py::structured` | LLM 结构化输出（解析/校验失败） | `stop_after_attempt(3)` + `wait_fixed(0)` |
| `app/services/search.py::search` | ddgs 搜索失败/无结果 | `stop_after_attempt(2)` + `wait_exponential(1,5)` + `reraise=True` |
| `app/services/fetch.py::_get` | 网页抓取 HTTP 错误 | `stop_after_attempt(2)` + `wait_exponential(1,5)` |

文章可用的观点：

- 三处失败性质不同（LLM 输出质量、搜索网络波动、目标站不稳定），但机制同一套，参数化即可——**重试是基础设施，不是业务逻辑**。
- `reraise=True` 的坑：search.py 里重试耗尽后要抛业务异常 `SearchError` 而非 tenacity 的 `RetryError`（提交 `480fc35 fix(app): reraise search errors instead of double-wrapping via RetryError`）——被调用方（agent 层）不该看到框架异常，端口边界只暴露领域异常。
- 等待策略选择：LLM 重试零等待（输出质量问题和网络拥塞不同，等也没用），外部服务重试指数退避（网络抖动常见，退避有效）。
