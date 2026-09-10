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

---

# P3 补充：tier3 路由的「抢匹配」回归 —— 关键词路由的假阳性失败模式

> 采集时间：2026-09-10（P3 收尾提交 `5ae1a9c` 之后，v2.0 分支）
> 数据说明：规则判定用真实代码 `classify_by_rules` 复跑（下方有原始输出）；修复前后代码见 commit `f907680` / `8dbbb7b`；行为已钉进 `tests/test_agent/test_router.py`。

## 背景：规则表新增第 6 类意图 browser_task（tier3）

P3 给意图系统加了浏览器任务意图，规则表位置（`router.py::_RULES`）：

```
weather → read_page → knowledge_qa → browser_task → search → chitchat
```

- `browser_task` 排在 knowledge_qa 之后（"什么是表单登录"这类教科书提问仍归 knowledge_qa）、search 之前（带站点/交互标记的任务比泛化的"搜索一下"更具体）。
- tier3 策略链是 `["browser_snapshot", "browser_open"]`（`graph.py::_STRATEGY_CHAIN`）——**没有 search 回退**：这些任务被当成"去页面上办事"，而不是"要一份资料"。
- 规则表位置=优先级的设计来自 P1（用顺序显式裁决关键词打架，"knowledge_qa 必须在 search 之前"）；P3 把它复制到了第 6 类意图。

## Bug：单字弱动词抢走 search 的既有匹配（评审发现）

初始实现（`f907680`）把 browser_task 写成一张纯关键词表（原文一词一行，此处压缩排版）：

```python
(
    "browser_task",
    "tier3",
    [
        # 交互动作（在页面上操作元素）
        "点击", "填写", "输入", "提交", "勾选", "选择", "登录", "注册", "表单", "搜索框",
        # 电商/站点（含技能的触发词：比价、购物、京东…）
        "比价", "购物", "下单", "加购", "加入购物车", "购物车", "结算", "商城",
        "京东", "淘宝", "天猫", "拼多多",
    ],
),
```

判定就是 `any(k in text for k in keywords)`。评审探针 5 例全部被抢成 tier3：

| 探针 | 抢它的词 | 抢走之前的既有匹配 | 修复前的下场 |
|------|---------|------------------|------------|
| 为什么选择 Python 而不是 Java | 选择 | search（关键词"为什么"） | tier3 链无 search 回退 → `all_strategies_failed` |
| 查一下怎么提交表单 | 提交 | search（关键词"查一下"） | 同上 |
| 选择哪个框架比较好 | 选择 | 无（规则层本不命中，交模型） | 同上 |
| Python 输入输出教程 | 输入 | 无 | 同上 |
| git 提交规范 | 提交 | 无 | 同上 |

为什么会被抢（两个原因叠加）：

1. **泛化动词做裸子串匹配**："选择/提交/输入"是日常提问里的高频词——"选择"出现在任何技术选型问题里，"提交"出现在任何 Git 提问里。
2. **规则顺序放大了假阳性**：browser_task 在 search 之前，"为什么选择…"本可命中 search 的"为什么"，但循环先撞上 browser_task 就 return 了。顺序裁决保证了"具体规则优先"，但"具体性"是靠词表声明的，不是靠匹配质量保证的。

用户可见性：tier3 没有 search 兜底，规则抢走 = 直接进一条没有回退的策略链，报 `all_strategies_failed`。这不是"分类错了但能救"，而是"用户看到一个本不该失败的失败"。**关键词路由的假阳性成本不对称**：假阴性回退到模型（可恢复），假阳性提交给无兜底的链（不可恢复）。

## 修复：强词/弱词两段式 + 页面上下文共现 + 求知守卫（commit `8dbbb7b`）

`browser_task` 的关键词位改成 `None`，由 `_is_browser_task` 显式求值（位置仍表达优先级）；判定拆三层（`router.py` 现文，压缩排版：省略类型标注与 docstring 全文）：

```python
_SITE_WORDS: tuple[str, ...] = ("比价", "购物", "下单", "加购", "加入购物车", "购物车",
                                "结算", "商城", "京东", "淘宝", "天猫", "拼多多")
"""电商/站点词（含技能触发词）：本身就是"去这个网站办事"的信号。"""

_BROWSER_STRONG_KEYWORDS = (*_SITE_WORDS, "搜索框", "浏览器")
_BROWSER_WEAK_KEYWORDS = ("点击", "填写", "输入", "提交", "勾选", "选择", "登录", "注册", "表单")

_BROWSER_CONTEXT_RE = re.compile(r"页面|按钮|表单|框(?!架)|网站|" + "|".join(map(re.escape, _SITE_WORDS)))
_BROWSER_INFO_SEEKING_RE = re.compile(r"怎么|如何|为什么|教程|规范|指南|入门")

def _is_browser_task(text: str) -> bool:
    if any(k in text for k in _BROWSER_STRONG_KEYWORDS):
        return True
    if _BROWSER_INFO_SEEKING_RE.search(text):
        return False
    return any(k in text for k in _BROWSER_WEAK_KEYWORDS) and bool(_BROWSER_CONTEXT_RE.search(text))
```

三个设计点：

1. **强词免上下文**：站点/电商词（"去这个网站办事"的明确信号）+ "搜索框"/"浏览器"——单独出现即 tier3。
2. **弱词要共现**：泛化交互动词必须与页面上下文标记同现（"点击登录按钮"）；`框(?!架)` 负向前瞻是关键细节——裸"框"算页面标记（输入框/搜索框），"框架"不算（"选择哪个框架比较好"是技术选型提问）。
3. **求知守卫**：`怎么|如何|为什么|教程|规范|指南|入门` 出现即弱词不成命中——问"怎么做"不等于"去做"。守卫只对弱词生效，强词（京东/比价）不受限。

注意没有改规则顺序，也没有增加任何模型调用：修复全部在判定条件里（多一次正则），"更聪明的规则"不等于"更贵的分类"。

## 验证：回到 pre-tier3 行为（真实复跑输出）

修复目标不是"调参到能过"，而是**与加 tier3 规则之前的判定一致**（测试注释给出的基准：`git show 9567a90:browsepilot/src/browsepilot/agent/router.py`）。用当前代码复跑 8 个样本的原始输出：

```
'为什么选择 Python 而不是 Java' -> ('search', 'tier1', 1.0)
'查一下怎么提交表单' -> ('search', 'tier1', 1.0)
'选择哪个框架比较好' -> None
'Python 输入输出教程' -> None
'git 提交规范' -> None
'帮我填写表单并提交' -> ('browser_task', 'tier3', 1.0)
'在页面上输入关键词' -> ('browser_task', 'tier3', 1.0)
'用浏览器帮我看看' -> ('browser_task', 'tier3', 1.0)
```

- 前两条回到 **search**（P1 就有的匹配，tier1/1.0）；后三条回到 **None**（规则层不命中，交模型兜底——与 pre-tier3 一致）。
- 后三个正样本证明共现/强词还能抓住真交互任务（只加守卫不加同现就只能等死）。
- 行为已钉进 `tests/test_agent/test_router.py`（`8dbbb7b` 新增）：`test_weak_interaction_words_do_not_steal_search`（参数化 2 例）、`test_weak_interaction_words_alone_stay_out_of_tier3`（参数化 3 例，含"框架"细节）、`test_weak_interaction_words_with_page_context_are_still_tier3`、`test_strong_site_words_need_no_context`。复跑：`uv run pytest tests/test_agent/test_router.py -q` → **19 passed**。
- 该回归的"不抢"直接约束来自 T4 任务书（"不得抢既有匹配"），4 条不抢测试在实现前就已通过（`task-4-report.md`）——说明它们 pin 的是既有行为，不是新行为；修复后全套 529 passed（`8dbbb7b`，progress 记录）。

## 残留与一般化教训

- **残留**（progress.md T4 记账）：强关键词仍会被问题式短语触发——"为什么京东比淘宝便宜" → tier3（京东是强词，求知守卫对强词不生效）。规则路由的调优没有终点，只有"用评测集说话"（P4 钩子）。
- **教训一**：给规则表加一条"更具体"的规则时，先问它的 precision 会不会伤到既有匹配；词表位置上的"具体"不等于判定上的"精确"。
- **教训二**：泛化动词（选择/提交/输入…）裸用就是假阳性机器；把"具体性"从词表升级为判定条件（强词/共现/守卫）是这一类的通用修复模式。
- **教训三**：规则抢走的代价取决于该规则背后的策略链——有兜底的意图抢错了还能降级，无兜底的意图（tier3）抢错就是硬失败。规则优先级设计应把"链的鲁棒性"也算进去。

## P3 代码位置（补充索引）

| 内容 | 位置 |
|------|------|
| tier3 规则引入（含被修复的裸关键词表） | commit `f907680`；`git show f907680 -- src/browsepilot/agent/router.py` |
| 强/弱词拆分 + 共现 + 求知守卫（修复） | commit `8dbbb7b`；`router.py::_SITE_WORDS`、`_BROWSER_STRONG_KEYWORDS`、`_BROWSER_WEAK_KEYWORDS`、`_BROWSER_CONTEXT_RE`、`_BROWSER_INFO_SEEKING_RE`、`_is_browser_task` |
| 两段式判定接入规则循环（keyword=None 分支） | `router.py::classify_by_rules` |
| tier3 策略链（无 search 兜底） | `graph.py::_STRATEGY_CHAIN` |
| 回归测试（5 探针 + 3 正样本） | `tests/test_agent/test_router.py` |
| pre-tier3 基准代码（期望值来源） | `git show 9567a90:browsepilot/src/browsepilot/agent/router.py` |
