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
