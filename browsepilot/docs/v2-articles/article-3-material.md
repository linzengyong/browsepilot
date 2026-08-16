# 文章 3 素材：《Agent 的"大脑"：意图识别系统的设计与演进》

> 采集时间：2026-08-03（P1 交付后，v2.0 分支）
> 文章主题：从"关键词+正则评分分类器"到"规则→模型→兜底"混合策略的演进；规则优先的理由与实测数据
> 数据说明：规则命中率用真实代码 `classify_by_rules` 实测（3 个评测场景 + 10 个手工样本）

## 叙事线：三阶段演进

### 阶段一：v1 关键词+正则评分分类器（旧代码，已删除）

代码位置：旧 `backend/app/agent/nodes.py`（V2 重构删除，可在 git 历史提交 `1326c9f` 查看原文）。

- `TOOL_RULES` 表（nodes.py:68）：每个工具一组 `keywords`（命中 **+1**）与 `patterns`（正则命中 **+2**）
- `classify_tool()`（nodes.py:107）：对每个工具打分，最高分 ≥ 阈值才采用，否则返回 None 走 LLM
- 阈值：`backend/app/config.py:65` — `tool_classify_threshold: int = Field(default=2, ge=1)`
- 作用位置：execute_node 内部，回答"这一步该调哪个浏览器工具"
- 命中后参数还不全 → 还要 LLM 补参数（`extract_args` 只覆盖 navigate/snapshot 少数工具）

```python
# 旧代码结构（nodes.py:68 起）
TOOL_RULES = {
    "browser_navigate": {
        "keywords": ["导航到", "访问", "打开", "跳转到"],
        "patterns": [r"https?://\S+", r"[a-z]+\.(com|cn|org)"],
    },
    "browser_click": {
        "keywords": ["点击", "按下", "提交"],
        "patterns": [r"点击.{0,20}(按钮|链接|搜索|提交)"],
    },
    "browser_press_key": {
        "keywords": ["按键", "按下", "回车", "Enter", "滚动", "翻页"],
        "patterns": [r"(按下|按键|回车|Enter|滚动|翻页)"],
    },
    ...
}
# classify_tool（nodes.py:107）：score += 1（关键词）/ += 2（正则），best_score >= 2 才采用
```

### 阶段二：v1 的教训——脆弱、不可解释

1. **关键词互相打架**：`browser_click` 与 `browser_press_key` 都含"按下"——"按下回车"两边都加分，谁赢取决于正则权重的微妙差异，改一个词就翻车。
2. **阈值是拍脑袋的魔法数字**：`default=2` 无依据、无 confidence 概念，不可调参、不可解释。
3. **决策不可解释**：日志只有 `"[execute_node] Classifier selected: {} (score >= {})"`（nodes.py:546），用户和开发者都不知道"为什么选它"。
4. **规则省下的调用又花回去**：规则选中工具后参数还得 LLM 补，两段式绕回 LLM。
5. 定位偏差：分类发生在"单步工具选择"层，没有任务级意图概念——计划里每步都重新猜。

### 阶段三：v2 混合策略——规则 → 模型 → 兜底

代码位置：`src/browsepilot/agent/router.py` + `graph.py::classify`。

| 层级 | 触发条件 | 结果 | confidence |
|------|---------|------|-----------|
| 规则 | 关键词/URL 正则命中 | 直接用，**零 LLM 调用** | 1.0 |
| 模型 | 规则未命中 | 小模型 structured output 分类（`IntentResult`） | 0.8 |
| 兜底 | 模型也失败（StructuredOutputError） | 强制 tier1/search | 0.0 |

与 v1 的本质区别：

- **意图分类上移到任务级**（classify 节点），不再在计划里逐步猜工具
- **工具+参数一次结构化提取**（execute 节点），没有"规则选工具、LLM 补参数"的两段式
- **兜底是"换一个必中的策略"**，不是"放弃"——对比 v1 熔断"3 次失败强制回答"（旧 backend/app/config.py:57）

## P1 实现细节

### 规则表结构（router.py::_RULES）

```python
_RULES: list[tuple[str, str, list[str]]] = [
    ("weather", "tier1", ["天气", "气温", "温度", "多少度", "下雨", "下雪", "晴", "阴", "weather", "台风", "空气质量"]),
    ("read_page", "tier2", ["打开", "访问", "读", "阅读", "摘要"]),
    ("knowledge_qa", "answer", ["解释", "什么是", "定义", "区别", "原理", "如何理解"]),
    ("search", "tier1", ["搜索", "搜一下", "查一下", "帮我找", "search", "是什么", "什么时候", "为什么", "怎么样"]),
    ("chitchat", "answer", ["你好", "嗨", "hello", "hi", "谢谢", "再见", "你是谁"]),
]
```

三个设计点：

1. **URL 正则特判**：`_URL_RE = re.compile(r"https?://[^\s，。！？\"]+")` 优先于规则表执行——"打开 https://… 告诉我内容"必须进 read_page，不依赖关键词运气，也不怕"打开"被别的意图抢走。
2. **规则顺序即优先级**：weather → read_page → knowledge_qa → search → chitchat。最关键的注释（router.py 原文）：
   > 注意 knowledge_qa 必须在 search 之前（"什么是"同时匹配两者）
   "搜索一下什么是 HTTP/2"同时命中 search 规则的关键词"搜索"与 knowledge_qa 规则的关键词"什么是"——真正的双命中，规则顺序决定路由结果：knowledge_qa 在前则归 knowledge_qa。注意这里不是子串关系（"什么是"并不是"是什么"的子串）：顺序之所以是裁决者，是因为两类规则的关键词表都可能同时命中混合意图的表述——这正是 v1 里"关键词打架"问题的解法：**不消除重叠，用顺序显式裁决**。
3. **空输入兜底 chitchat**（`classify_by_rules` 开头 `if not text`）。

### IntentResult 的 Literal 约束（structured output 保证枚举合法）

```python
class IntentResult(BaseModel):
    """用户任务意图分类结果。intent 取值：weather/search/read_page/chitchat/knowledge_qa；tier 取值：tier1/tier2/answer。"""

    intent: Literal["weather", "search", "read_page", "chitchat", "knowledge_qa"]
    tier: Literal["tier1", "tier2", "answer"]
```

- 模型路径走 `provider.structured(schema=IntentResult, model=small_model)`——forced tool calling + Pydantic 校验（文章 2 主题），模型**不可能**返回枚举外的值：非法值触发重试，重试耗尽抛 StructuredOutputError → 走兜底
- 结论：**系统不可能产生未知 intent/tier**。枚举即契约，路由层零"else 分支"，execute 节点可以放心 `_TOOL_BY_INTENT[state.intent]` 直接索引
- 顺带一提：`RouteDecision.confidence` 目前只写入 Tracer 决策事件（`event_type="decision"`，graph.py::classify），不参与分支判断——这是 P4 钩子（见下）

### 兜底 tier1/search 的决策理由

graph.py::classify 中 StructuredOutputError 分支的注释原文：

```python
except StructuredOutputError:
    # fallback: search is the safest default for an info product
    decision = RouteDecision(intent="search", tier="tier1", confidence=0.0)
```

- 产品定位是**信息产品**（查天气/查资料/读网页）：搜索总能返回点什么，答非所问是最差结果
- 兜底不是降级到"不知道"，而是降级到"必中的策略"
- 与 v1 对比：v1 熔断后"按照现有搜集到的材料组织回答"（旧 nodes.py:983-986）——用户看到的是答非所问；v2 是"问题暴露 + 换策略"

### 降级原则（execute 节点，与文章 2 呼应）

- 参数提取失败 → `param_extraction_failed` 明确错误，answer 节点原文转达："抱歉，这个任务暂时无法完成：{message}。可以换个说法试试。"（graph.py::answer）
- 不编造结果——结构化输出的可靠性链条（文章 2）直接支撑这里的降级质量

## 数据：规则命中率（2026-08-03 实测）

方法：用真实代码 `classify_by_rules` 跑 **3 个评测场景 + 10 个手工样本**（含 1 个故意落在规则外的样本），统计规则命中 vs 模型回退。

结果：**12/13 规则命中（92.3%），1/13 模型回退**。

3 个评测场景（全部规则命中）：

| 场景 | 输入 | 结果 |
|------|------|------|
| p0-weather | 北京今天天气怎么样？ | 规则命中 → weather/tier1 |
| p0-search | 搜索一下 Python 3.13 正式发布时间 | 规则命中 → search/tier1 |
| p0-read-page | 打开 https://www.python.org 告诉我页面主要内容 | 规则命中（URL 特判）→ read_page/tier2 |

10 个手工样本（9 命中 / 1 回退）：

| 输入 | 结果 |
|------|------|
| 上海明天多少度？ | 规则命中 → weather |
| 广州下雨吗 | 规则命中 → weather |
| 帮我查一下今天的天气 | 规则命中 → weather |
| 搜一下 Rust 异步编程教程 | 规则命中 → search |
| 帮我找找 OpenAI 的最新公告 | 规则命中 → search |
| 什么是最小可行产品 MVP | 规则命中 → knowledge_qa |
| 解释一下量子纠缠 | 规则命中 → knowledge_qa |
| 打开 https://news.ycombinator.com 看看热帖 | 规则命中（URL 特判）→ read_page |
| 你好 | 规则命中 → chitchat |
| 帮我处理一下那个事情 | **模型回退**（无任何关键词，规则外表达） |

配套数据：eval mock 模式 3/3 PASS（`uv run python -m eval.run_eval --mock`）：

```
[browsepilot eval v0.2] 3 scenarios loaded
  [PASS] p0-read-page: keywords hit=['Python']
  [PASS] p0-search: keywords hit=['2024']
  [PASS] p0-weather: city='北京' expected='北京'; temperature_c=26.0 (number)
[eval] 3/3 passed
```

### 零 LLM 成本的天气路径（精确边界）

- **classify 阶段零 LLM**：天气关键词覆盖面广（天气/气温/温度/多少度/下雨/下雪/晴/阴/台风/空气质量/weather），规则命中直接出意图，不调模型——上面 13 个样本里所有天气类全部命中
- **精确边界**：整条天气链路仍有一次参数提取调用（execute 节点 `provider.structured` 提取 `WeatherParams.city`）和一次回答合成调用（answer 节点）——"零 LLM"目前只对**意图分类**成立，不是整条链路
- **P4 演进点**：天气参数也改规则提取（"XX天气"正则抓城市名），天气链路可做到端到端零 LLM——写文章时可以把这个"边界"作为讨论点，比宣称"零成本"更有说服力

## 代码位置索引（供引用）

| 内容 | 位置 |
|------|------|
| v1 评分分类器（旧，已删，git 1326c9f 可查） | `backend/app/agent/nodes.py:68`（TOOL_RULES）、`:107`（classify_tool）、`:546`（选中日志） |
| v1 阈值（默认 2） | `backend/app/config.py:65` |
| v1 熔断降级（对比用） | `backend/app/config.py:57`、`backend/app/agent/nodes.py:983-986` |
| v2 规则表 + URL 特判 + IntentResult | `src/browsepilot/agent/router.py` |
| 混合分类（规则→模型→兜底） | `src/browsepilot/agent/graph.py::classify` |
| 兜底理由注释 | `graph.py::classify`（StructuredOutputError 分支） |
| 降级回答 | `graph.py::answer` |
| 规则测试 | `tests/test_agent/test_router.py` |
| 模型回退测试 | `tests/test_agent/test_graph.py::test_graph_falls_back_to_model_when_rules_miss` |
| 评测场景 | `eval/scenarios/p0_weather.yaml`、`p0_search.yaml`、`p0_read_page.yaml` |
| 评测 runner（check_outcome: field/contains_keyword；--mock） | `eval/run_eval.py` |
| 意图枚举与 tier 契约 | `router.py::IntentResult`（Literal） |

## 留给 P4 的钩子

1. **置信度阈值参数化**：confidence（规则 1.0 / 模型 0.8 / 兜底 0.0）已进 tracer 但未参与分支；P4 可加"低置信度转 HITL / 换强模型"策略。router.py 模块 docstring 已声明此意图："Confidence thresholds are deliberately deferred to P4 (tune against eval data, not gut feeling)"。
2. **评测数据驱动的规则调整**：本次 12/13 是"评测场景 + 人工样本"的小样本；P4 用评测集回归，规则增删以"评测通过率不下降"为准，拒绝拍脑袋加关键词。
3. **规则与模型的分工边界**：目前规则覆盖 5 类意图；新增意图（如"对比"、"翻译"）先加规则还是先进模型，需要评测数据说话。
4. **天气链路端到端零 LLM**（见上）：参数提取规则化后，Tier1 天气可完全脱离模型运行，成本与延迟双降。
