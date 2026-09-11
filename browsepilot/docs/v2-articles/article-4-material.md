# 文章 4 素材：《Agent 的"体检"：如何构建评测集与评测体系》

> 采集时间：2026-09-11（P4 Task 9 交付后，v2.0 分支；P4 提交范围 `ac3286d..HEAD`）
> 文章主题：评测集怎么从 3 个场景长到 16 个、schema 每个字段买到了什么、断言类型的取舍、3 轮真实量化数据、mock 回归门禁，以及两个"评测抓 bug"的真实案例（虚假通过红线 / 评测→修复闭环）
> 数据说明：**所有数字/引文都能追到产物**——`docs/v2-eval.md` §7（量化结果）与其来源 `data/eval-reports/p4-t7*.json|md`、`docs/v2-p1-report.md`、`docs/v2-p2-report.md`、git commit、`uv run pytest` 实测计数、SDD ledger（`.superpowers/sdd/2026-08-16-browsepilot-v2-p4/progress.md`）。拿不到/未验证的一律标注"未采集 / 未验证"，不估算。
> **口径声明（读任何数字前先读这五行）**：① token 是 **chat-only 下限**（structured/stream 没有 usage 面，30/63 次模型调用有计量）；② P95 含**依赖网络**时间（P50 才是产品侧读数）；③ 3 轮 = **同一场景集重复跑**（量波动，不是跨模型/跨日抽样）；④ **cost 未采集**；⑤ mock 与 real 数据不可混用（mock 只证明断言与管线可复现，不代表模型质量）。

## 0. 数据在哪 / 怎么复现（给作者与读者）

| 产物 | 位置 | 内容 |
|------|------|------|
| 场景集（入库） | `eval/scenarios/*.yaml`（16 个文件） | 声明式场景：prompt + tier + tags + mode + checks |
| 断言引擎 | `eval/checks.py`（纯函数、无 I/O） | 10 种检查类型；`eval/run_eval.py::check_outcome` 是兼容薄适配层 |
| 真实模式 harness | `eval/harness.py` | BrowserPool 注入 + 逐场景 `executor.aclose()` 隔离 + token/耗时/工具名采集 |
| 统计与报告 | `eval/report.py`、`eval/run_eval.py --json/--md/--repeat` | 成功率/P50/P95/token/per-tier/跨轮方差/flaky |
| 门禁 | `eval/gate.sh`、`.github/workflows/ci.yml` | 本地一键 = CI 同口径（mock） |
| 评测体系文档（入库） | `docs/v2-eval.md`（501 行） | §3 哲学 · §4 场景规范 · §5 检查类型 · §6 运行指南 · §7 3 轮量化结果 · §8 已知限制 |
| 量化产物（gitignored） | `data/eval-reports/p4-t7.json|md`（3 轮 acceptance）、`p4-t7-full.json|md`、`p4-t7-hitl.json|md`、`p4-t5*.json`、`t5-fix.json`、`*-run.log`、`t7-probe-*.txt` | 文章引用的数字都以此为准 |
| 真实 trace 语料（gitignored） | `data/traces/*.jsonl`（含 `t5fix-real-verbatim.jsonl`、`probe-REAL-FAILURE-*.jsonl`） | 降级链/失败形态的原始事件流 |

```bash
cd browsepilot
uv run pytest tests/ -q                                    # 852 passed（2026-09-11 实测）
bash eval/gate.sh                                          # 本地一键门禁（pytest+ruff+mypy+eval mock）
uv run python -m eval.run_eval --mock                      # 离线：passed=8 skipped=4 disabled=4
# 真实 3 轮（需 BP_MODEL_API_KEY + BP_BROWSER_CHANNEL=msedge；本轮跑法见 v2-eval.md §6.4）
BP_DEFAULT_MODEL=deepseek/deepseek-chat BP_MODEL_API_KEY=... BP_BROWSER_CHANNEL=msedge \
  uv run python -m eval.run_eval --tag acceptance --repeat 3 --md data/eval-reports/
```

---

## 1. 评测集设计

### 1.1 场景集演进：v0.2 的 3 个场景 → v2 的 16 个文件

| 阶段 | 形态 | 数量 | 关键变化 | 出处 |
|------|------|------|----------|------|
| v0.2（P1 建立，P2/P3 沿用） | `p0-weather` / `p0-search` / `p0-read-page`；3 种检查（`field` / `contains_keyword` / 未知类型→失败）；手动跑、无量化 | 3 | 只覆盖"顺利的一天"：happy path 三连 | `docs/v2-eval.md` §2；`docs/v2-p1-report.md` §二 |
| P4-T2（`be3e9a0`） | schema v2 + 场景集扩充：tier1/2/3 + HITL + 多轮 + 降级 | 16 文件 | 每条能力面都有正例/负例；`tags`/`mode`/`timeout_seconds`/`fragile`/`enabled` 全部落地 | `eval/scenarios.py`；commit `be3e9a0` |
| P4-T3（`b86d5ed`） | 多轮接线 | — | `multi_turn_context` 从 `enabled: false` 启用（harness 注入 history） | commit `b86d5ed` |
| P4-T5（`61e1b98`） | p0-* 归档 | 16（3 条 p0-* 转 `enabled: false`） | 历史基线**不删**：断言逐条移交新场景，归档仅供跨阶段对比 | commit `61e1b98`；`test_p0_assertion_semantics_have_enabled_successors` |
| 现状 | 16 文件 = **8 可 mock 执行 + 4 mock 下 SKIP（real-only）+ 4 停用（3 归档 + 1 待启用）** | 16 | `--mock` 日志：`passed=8 skipped=4 disabled=4` | `docs/v2-eval.md` §4.6 / §6.1 |

**"归档不是删除"的证据链**（文章可讲的设计点）：`p0-weather` 的 `field temperature_c (kind: number)` 断言逐条迁入 `t1_weather_beijing`；`p0-search` 拆成结构版 `t2_search_structural` 与内容版 `t2_search_content`；`p0-read-page` 由 `t2_read_page` 承接。测试 `test_p0_assertion_semantics_have_enabled_successors` 把"缺承接者 = 存档变成丢断言"钉死（`_P0_SUCCEEDED_BY` 映射表，测试内定义）。

当前 16 个文件（逐字由 `load_scenarios()` 读出，见 `docs/v2-eval.md` §4.6）：

| id | tier | mode | tags | 状态 | fragile | 断言类型 |
|----|------|------|------|------|---------|----------|
| `degrade_browser_unavailable` | tier3 | mock | degrade | 运行中 | — | `tier_is` / `tools_called` / `contains_keyword` / `not_contains` |
| `hitl_login_detect` | tier3 | real | hitl, browser | 运行中 | — | `needs_human` / `tools_called` |
| `multi_turn_context` | tier1 | real | acceptance | 运行中 | — | `contains_keyword` |
| `p0-read-page` / `p0-search` / `p0-weather` | tier2/1/1 | both | acceptance | **归档**（`enabled: false`） | `p0-search` 标 fragile | `contains_keyword` / `field`（原样保留） |
| `t1_weather_beijing` / `t1_weather_shanghai` | tier1 | both | smoke, acceptance | 运行中 | — | `tier_is` / `field` ×2 / `tools_called` / `degradation_absent` / `stop_reason_is` / `contains_keyword` |
| `t1_weather_not_found` | tier1 | both | smoke, acceptance | 运行中（负例） | — | `not_contains` / `contains_keyword` |
| `t2_read_page` / `t2_read_page_failure` | tier2 | both | smoke, acceptance | 运行中（正/负例） | — | `tools_called` / `contains_keyword` / `degradation_absent` / `stop_reason_is`；失败版 `not_contains` |
| `t2_search_structural` | tier1 | both | smoke, acceptance | 运行中 | — | `tools_called` / `degradation_absent` / `stop_reason_is` |
| `t2_search_content` | tier1 | both | acceptance | 运行中 | ✓ | `contains_keyword` |
| `t3_browser_open` / `t3_browser_snapshot` | tier3 | real | browser, acceptance | 运行中 | — | `tier_is` / `tools_called`（含 `exact_sequence`）/ `contains_keyword` / `degradation_absent` / `stop_reason_is` / `not_contains` |
| `t3_browser_interaction` | tier3 | real | browser, acceptance | **待启用**（`enabled: false`，P3 交互链未入图） | — | `tools_called` / `contains_keyword` / `degradation_absent` |

### 1.2 schema 设计：每个字段买到了什么（字段/默认值逐字取自 `Scenario.model_fields`，见 v2-eval.md §4.1）

| 字段 | 默认 | 文章论点（为什么需要它） |
|------|------|--------------------------|
| `id` / `name` / `prompt` | 必填 | `prompt` 必须非空（strip 后）；`id` 全目录唯一（重复 → 报告指出两个文件名：同一场景跑两遍会悄悄污染成功率） |
| `tier` | `tier1` | `Literal["tier1","tier2","tier3"]`：**路由的实际层级**（search 规则命中就是 tier1，哪怕文件名叫 `t2_*`）；拼错在加载期报错 |
| `tags` | `["acceptance"]` | 门禁分层：`smoke`（CI 必跑的确定性核心）/ `acceptance`（验收全量）/ `browser` / `hitl` / `degrade`；未知 tag 报错并列合法集。**缺省 = acceptance**，写文章时别把它当"全跑" |
| `mode` | `both` | `mock` / `real` / `both`；与本次运行不匹配 → `[SKIP]`，**不计 pass/fail、不进 exit code**——"跑不了"（mock 没有浏览器）不是"跑失败了" |
| `checks` | `[]` 但**必须非空** | 没有断言的场景 = 必然通过的场景（虚假通过），加载错误 |
| `timeout_seconds` | `120.0`（`gt=0`） | 每轮墙钟预算，超时抛 `TimeoutError`（消息带 id/轮次/预算）；0 或负数报错 |
| `fragile` | `False` | **断言依赖外部动态内容**：失败照实打 FAIL、单列 `fragile_failed`、**不改变 exit code**。语义是"对环境的依赖被显式承认"，不是"放宽通过标准"（详见 §1.4） |
| `enabled` | `True` | `False` → `[DISABLED]`：列出、不跑、不进统计（P3 遗留的交互链用这个口子声明） |
| `follow_up` | `[]` | 第 2..n 轮 prompt；`checks` 评估**最后一轮**的 outcome；token/耗时是**整场景各轮累加**——同一场景两种口径，写文章引用时要注意 |

**fail-loud 的 schema 底座**（文章"评测不自我欺骗"的起点）：`Scenario` 与 `Check` 都 `extra="forbid"`（`mdoe: real`、`keyword:` 漏 s 这类笔误一律报错）；空 checks / 空 prompt / 非正 timeout / 重复 id / 空目录 → `ScenarioLoadError`（带文件名）。另有跨字段校验：**prompt 必须命中它声明的 tier**（`test_every_scenario_prompt_routes_to_its_declared_tier`，离线可跑）——tier3 提示词不得含 `http(s)://` 或"打开/访问/读/阅读/摘要"（会被 `read_page` 规则先抢走），要靠"浏览器/搜索框"这类强触发词落 tier3。

### 1.3 断言类型表（10 种，实现 = `eval/checks.py::run_check`）

| `type` | 语义（压缩） | 数据来源 | 实际用在什么场景 |
|--------|--------------|----------|------------------|
| `field` | `result_data[field] == expected`；`kind: number` 只断言"是数值"（`bool` 不算数值） | `outcome["result_data"]` | 天气城市/温度（`t1_weather_beijing` / `shanghai`）——**数据断言**：只有工具真成功才有这个字段 |
| `contains_keyword` | `final_answer` 含**任一** keyword | `final_answer` | 正例可读性 + 负例的"诚实失败措辞"（"无法/抱歉"） |
| `not_contains` | `final_answer` 不含**任何** keyword | `final_answer` | 反编造：`t1_weather_not_found` 不得出现 `°C`；t3 不得出现失败模板词 |
| `tools_called` | `contains`（默认，全出现）/ `not_contains` / `exact_sequence`（**有序子序列**） | harness 注入的 `outcome["tool_calls"]`（**含失败尝试**，多轮累加） | tier3 观察链顺序（`exact_sequence: [browser_open, browser_snapshot]`）、search 结构断言 |
| `tier_is` | `outcome["tier"] == expected` | run_task state（路由**实际**层级） | 每条正例：证明走到了预期层级 |
| `stop_reason_is` | `outcome["stop_reason"] == expected` | run_task state | 正例 `completed`；抓 partial（只导航未观察） |
| `tokens_below` | 总 token ≤ `limit` | `token_usage.total_tokens` | **有实现与单测、尚无场景声明**（chat-only 下限直接当硬预算会误判，见 §2.4） |
| `latency_below` | 总耗时 ≤ `limit_ms` | `duration_ms`（整场景墙钟） | 同上，**尚无场景声明** |
| `degradation_absent` | `degradation_log` 为空 | run_task state | 每条正例：策略链一次失败都不许有（**全失败时 `stop_reason` 仍是 `completed`，只有这条抓得住**） |
| `needs_human` | `pending_human_action` 为真 | run_task state | HITL 场景（interrupt 发生在 answer 之前，`final_answer`/`stop_reason` 尚空，不能断言它们） |

**取舍的设计点**（文章可展开）：

1. 当前 16 个文件实际用到 **8 种**；`tokens_below` / `latency_below` 是"预留的预算门禁"——引擎就绪、单测覆盖，但**没有场景声明**（好素材：指标先于门禁到位是失败路径，不是成功路径）。
2. 未知类型 / 未知 mode → `passed=False` + 明确 detail，**不抛异常**（一个笔误不该炸掉整轮评测）；`eval/run_eval.py::check_outcome` 保留 tuple 形状的兼容薄层，既有调用方式不变。
3. 精确边界（写文章别说过头）：`tier_is` 未强制 `expected` 时 `None == None` 会通过（当前 16 个文件全部显式声明）；`not_contains` 在 answer 缺席时按空串处理 → **通过**（所以"不得编造"类断言必须与"必须有数据"成对出现）；`degradation_absent` 缺席与空列表同判通过（正向断言）。

### 1.4 断言与动态内容解耦（P4-D6）：结构断言 vs 内容断言的具体做法

**原则**：断言只允许依赖两类东西——**产品结构**（工具链、路由层级、终止原因、降级记录、数据字段、用量/耗时预算）与**数据断言**（如天气的 `field city`）。不允许依赖外部动态内容的**具体值**（搜索结果条目、页面正文、模型措辞）。否则"外部数据漂移"会被读成"代码回归"。

同一句 prompt（"搜索一下 Python 3.13 正式发布时间"）在两个场景里的两种断言，是这条原则最干净的对照：

```yaml
# t2_search_structural.yaml（结构版；可 gate）
checks:
  - type: tools_called
    tools: [search]
  - type: degradation_absent      # 全策略失败 → 必挂
  - type: stop_reason_is
    expected: completed           # partial（只导航未观察）→ 必挂

# t2_search_content.yaml（内容版；fragile，失败不计 exit code）
fragile: true
checks:
  - type: contains_keyword
    keywords: ["2024"]            # 依赖 ddgs 返回里含 "2024"
```

**"删断言前先移交"的实例**：结构版原有一条 `contains_keyword ["http"]`（"答案里出现结果链接"），P4-T5 真实抽查发现它在真实模式**稳定 FAIL**（answer 的 system prompt 从未要求回显链接）；修法是**删掉并移交给内容版**，而不是放宽阈值。`test_structural_search_scenario_does_not_assert_on_answer_wording` 钉住"结构版不得写措辞断言"——防的是它被改回去。

**fragile 的运行语义**（`eval/run_eval.py` 原文日志格式）：

```
[FAIL] t2_search_content: keywords hit=[] (fragile=true — 不计入门禁 exit code)
```

即：失败照实 FAIL、单列 `fragile_failed` 计数、报告单独统计——**它是"断言对环境的依赖被显式承认"，不是"放宽通过标准"**。

---

## 2. 量化数据（3 轮真实运行，T7/T8）

> 数据来源：`data/eval-reports/p4-t7.json|md`（acceptance 3 轮，2026-09-11T19:54:28+08:00，`environment.git_commit=61a6a28`）+ `p4-t7-run.log`。下述数字与 `docs/v2-eval.md` §7 逐字一致（T7 记账：脚本对 35/35 项自动交叉检查过 §7 与 JSON 的一致性）。

### 2.1 运行前提与形状（"这份数据在什么条件下成立"）

| 项 | 值 |
|----|----|
| 时间窗 / 耗时 | 19:49:50（池启动）→ 19:54:28（报告落盘）＝ **4 分 38 秒** |
| 模式 / 模型 | real / `deepseek/deepseek-chat`（必钉：思考模型拒绝 forced tool_choice → structured 全场景报错） |
| 浏览器池 | `started`（headless=True, channel=msedge, max_pages=3）；跨 3 轮只起一次（不把起停噪声混进方差） |
| 运行形状 | `--tag acceptance --repeat 3`：10 条可执行场景 × 3 轮 = **30 次场景运行**；每轮 skipped=2（mock-only + 无 acceptance tag）+ disabled=4（3 归档 + 1 待启用） |
| exit code | **0**（无非 fragile 失败；1 条 fragile 失败只计数） |

### 2.2 3 轮结果 + 跨轮方差

| 轮次 | 执行 | 通过 | 失败（门禁） | fragile 失败 | 成功率 | P50 (ms) | P95 (ms) | 总 token |
|------|------|------|--------------|--------------|--------|----------|----------|----------|
| 1 | 10 | 9 | 0 | 1 | 90.0% (9/10) | 3893.5 | 20053.2 | 3427 |
| 2 | 10 | 9 | 0 | 1 | 90.0% (9/10) | 3173.4 | 28910.6 | 3398 |
| 3 | 10 | 9 | 0 | 1 | 90.0% (9/10) | 3221.3 | 33314.8 | 3366 |
| **样本池（30 次运行）** | **30** | **27** | **0** | **3** | **90.0% (27/30)** | **3221.3** | **33380.9** | **10191** |

样本池延迟 min / max = **1772.3 / 45503.1 ms**；30 次运行的 `stop_reason` 全部 `completed`（无 partial / error）；日志 **0 条** `EXCEPTION` 行。

| 指标 | 最小 | 最大 | 均值 | 标准差（总体） | 读数 |
|------|------|------|------|----------------|------|
| 成功率 | 0.9000 | 0.9000 | 0.9000 | **0.0000** | 3 轮结论完全一致（每轮都是同一组 9 PASS + 1 fragile FAIL） |
| 延迟 P50 (ms) | 3173.38 | 3893.53 | 3429.40 | **328.77** | ≈ 均值 9.6%，相对稳定（产品侧读数） |
| 延迟 P95 (ms) | 20053.17 | 33314.83 | 27426.20 | **5514.86** | ≈ 均值 20.1%，被依赖延迟拉伸（引用时别只贴 P95） |
| 总 token | 3366 | 3427 | 3397 | **24.91** | ≈ 均值 0.7%，模型侧调用量跨轮恒定 |

**flaky 场景 = 0 个**（报告 `flaky: []`）：10 条执行场景在 3 轮里状态完全一致；唯一失败 `t2_search_content` 是 **3 轮全 FAIL（一致失败）**，不是 flaky。文章论点：**稳定的通过集合可以复现，稳定的失败集合也可以归因**——3 轮同因同果正是 P1"天气 100% 可靠"那类单轮结论所缺的方差证据（P1 的单轮读数见 `docs/v2-p1-report.md` §二/§六；P4 用这条 3 轮证据把"100% 可靠"升级成"可复现的通过集合 + 波动读数"，`docs/v2-eval.md` §3.5）。

**分层（样本池口径）**：tier1 83.3%（15/18，3 条 fragile 失败全在这层）、tier2 100%（6/6）、tier3 100%（6/6）。P50/P95：tier1 11609.65 / 43327.11 ms；tier2 3213.58 / 4340.56；tier3 2378.60 / 3090.64 —— **tier3（真浏览器）反而最快**，因为它的路径不依赖 ddgs 网络（对照 tier1 的搜索/兜底）。

### 2.3 逐场景 × 逐轮明细（出处：`docs/v2-eval.md` §7.2，数字逐字一致）

| 场景 | tier | R1 | R2 | R3 | token R1/R2/R3 |
|------|------|----|----|----|----------------|
| multi_turn_context | tier1 | PASS · 15175 | PASS · 6808 | PASS · 5495 | 236 / 234 / 238 |
| t1_weather_beijing | tier1 | PASS · 2826 | PASS · 2854 | PASS · 2465 | 101 / 99 / 102 |
| t1_weather_not_found | tier1 | PASS · 12129 | PASS · 11760 | PASS · 17358 | 930 / 857 / 932 |
| t1_weather_shanghai | tier1 | PASS · 3144 | PASS · 2422 | PASS · 2509 | 100 / 103 / 101 |
| t2_read_page | tier2 | PASS · 4623 | PASS · 3493 | PASS · 3144 | 597 / 556 / 567 |
| t2_read_page_failure | tier2 | PASS · 3164 | PASS · 2624 | PASS · 3263 | — / — / — |
| t2_search_content | tier1 | **FAIL** · 21694 | **FAIL** · 11460 | **FAIL** · 18418 | 430 / 494 / 433 |
| t2_search_structural | tier1 | PASS · 18048 | PASS · 42943 | PASS · 45503 | 517 / 511 / 496 |
| t3_browser_open | tier3 | PASS · 2824 | PASS · 2327 | PASS · 3179 | 248 / 279 / 247 |
| t3_browser_snapshot | tier3 | PASS · 2430 | PASS · 1947 | PASS · 1772 | 268 / 265 / 250 |

观察点（都能在表里核对）：
- `t2_search_content` 3 轮 FAIL 明细都是 `keywords hit=[]`（依赖漂移，见 §7）；
- `t2_search_structural` 是 P95 的主要贡献者（18.0s → 42.9s → 45.5s）——**同一份结构断言在 3 轮里过山车，说明波动来自依赖而不是断言**；
- `t2_read_page_failure` 3 轮 token 均缺席：它走模板答案路径（0 次模型调用）——"未知不是 0"的如实处理，不是采集故障。

### 2.4 诚实边界（写文章必须带上的"读数限制"）

- **token 是 chat-only 下限**：`structured` / `stream` 调用没有 usage 面 → 只有 `chat` 计入。3 轮样本池 **30/63 次模型调用有计量（47.6%）**，`total_tokens=10191` 是**下限**而不是"总消耗"；报告每行带 `measured_calls` / `model_calls`（诚实分母），每轮 `token_samples=9/10`。
- **cost 未采集**：3 轮 `cost_samples=0`、`total_cost_usd=null`。原因：每次运行启动时 LiteLLM 取远端价格表超时（日志 `Failed to fetch remote model cost map …: timed out. Falling back to local backup`），本地备份也没有该模型价格 → 写"未采集"，不估算。
- **P95 含依赖网络时间**：P95 3 轮 20.05s → 33.31s（stdev 5514.86），单条极值 45.5s（R3 `t2_search_structural`）。P50（3.17–3.89s）才是相对稳定的产品侧读数。
- **样本量**：3 轮 × 10 场景 = 30 次运行，每场景 3 次。这是**同一场景集重复跑**的波动，不是跨模型 / 跨日 / 跨网络的抽样——换模型或换网络出口结论不保证迁移。
- **mock 数字不代表模型质量**：CI 门禁跑 FakeProvider/FakeExecutor，只能证明断言与管线可复现（报告里有 mock banner）。
- **P50/P95 是线性插值**（numpy 默认口径）：空集 → `None`，n=1 → 该样本本身；样本数由 `latency_samples` 暴露（样本池 30；per-tier 18/6/6）。

### 2.5 3 轮之外的补充运行（取证用，别混进 3 轮结论）

| 运行 | 命令形状 | 结果 | 耗时 | exit |
|------|----------|------|------|------|
| 冒烟先行 | `--tag smoke --repeat 1` | passed=6 failed=0 fragile_failed=0 skipped=6 disabled=4 | 81s | 0 |
| 全量一轮 | 无 tag，`--repeat 1` | executed=11，passed=9 failed=**1**（`hitl_login_detect`）fragile_failed=1，成功率 81.8%，P50 4828.85 / P95 20007.47 | 1m36s | **1** |
| HITL headful 专跑 | `--tag hitl --repeat 1`（`BP_BROWSER_HEADLESS=false`） | executed=1，passed=0 failed=1，耗时 29033.35 ms，tokens **未采集**（该场景 0 次模型调用） | 30s | **1** |

冒烟先行的理由（工程细节，文章可用）：把"凭据/网络/浏览器池是否可用"和"3 轮验收"分开——冒烟 6/6 之后才开 3 轮，否则失败会混进环境噪声。

---

## 3. 回归门禁

### 3.1 `eval/gate.sh`：本地一键 = CI 同口径

```bash
cd browsepilot && bash eval/gate.sh    # 或仓库根：bash browsepilot/eval/gate.sh
```

`set -euo pipefail`：任一步失败即停 + 非零退出；全绿时末尾打 `[gate] ALL PASS`。开头打印环境行（cwd / 分支 / HEAD 短号 / uv 版本 / 时间）——CI 或本机失败时，日志本身能回答"哪份代码、哪个环境"。

| 步骤 | 命令 | 说明 |
|------|------|------|
| 1 | `uv run pytest tests/ -q` | 全量单测（P4 收口 **852 passed**，2026-09-11 实测） |
| 2 | `uv run ruff check src/ eval/ tests/` | lint（`All checks passed!`） |
| 3 | `uv run mypy src/browsepilot/` | 类型检查（`Success: no issues found in 32 source files`） |
| 4 | `uv run python -m eval.run_eval --mock` | 评测回归（`passed=8 skipped=4 disabled=4`，exit 0） |

CI（`.github/workflows/ci.yml`，push `master`/`v2.0` + PR）的步骤与 gate.sh 逐步一致：Install → Lint → Types → Tests → **Eval (mock)**。

### 3.2 为什么 mock 进 CI、real 不进（P4-D8）

- CI 环境约束：**无网络、无模型 key、无浏览器**——真实模式跑不了。
- 更根本的理由：真实结果受 **ddgs 结果漂移**影响会 flaky（P2 D1 / P4 §7.5 A1 实证），**把 flaky 接进必过门禁等于把门禁变成噪声源**。
- 真实模式 = 手动/验收运行（可选定时任务）：`--tag acceptance --repeat N`，绝不进 CI。
- 文章论点：门禁的第一职责是"可信"而不是"全面"——一个每次都红的门禁和一个从不跑的门禁，工程价值相同。

### 3.3 判定规则：SKIP / DISABLED / 空跑 / fragile 各走哪条路

| 状态 | 判定 | 理由 |
|------|------|------|
| `mode` 不匹配 | `[SKIP]`，不计 pass/fail、不进 exit code | "跑不了"不是"跑失败了"；环境不匹配不能污染成功率 |
| `enabled: false` | `[DISABLED]`（列出、不跑、不进统计） | P3 遗留的交互链用它声明"未启用" |
| `--tag` 选不中任何可运行场景 | **exit 1** | 空跑全绿与"跑过且全绿"在日志上无法区分 |
| fragile 失败 | 计数 + 打印，**不 gate** | 内容漂移不是回归（§1.4） |
| 非 fragile 失败 / 场景异常 | exit 1；异常打 `[FAIL] … EXCEPTION …` 且**不中断**后续场景 | 一轮评测里一条场景炸掉不该掩盖其他场景的结果 |

### 3.4 fail-loud：缺数据 → FAIL，不是 0（四层落点）

文章可用"每一层都在拒绝同一件事：把不知道当成 0"来串：

- **断言层**：`tokens_below` / `latency_below` / `tools_called` 拿不到数据时给可诊断的 FAIL（detail 指明缺哪个注入字段）；`field` / `contains_keyword` 缺声明同样是 FAIL；
- **采集层**：harness 采不到用量时 `token_usage` 字段**缺席**（不填 0）；provider 只报 total 时 `input_tokens` / `output_tokens` 缺席；`cost_usd` 只在 > 0 时出现（0 多半是"算不出价"）；
- **聚合层**：`None` 不进样本、不填 0；样本数为 0 时 JSON 写 `null`、MD 写"—（未提供）"；
- **加载层**：未知 tag/字段/mode、空 checks、非正 timeout、重复 id、空目录 → `ScenarioLoadError`（报文件名）。

---

## 4. 最硬的素材（一）：**虚假通过**的红线 —— t3 场景曾在"全失败"运行上 PASS

> 出处：P4-T1+T2 合并审查（C1 Critical）→ 修复 commit `dddb99b`（`fix(eval): harden scenario assertions against failed/partial runs`）；回归网测试在 `tests/test_eval/test_scenarios.py`（当前 23 个守卫用例）；场景 YAML 顶部注释保留了当时的断言与判决。

### 4.1 症状：两条机制叠加，让"只断言工具名 + 关键词"的正例场景全绿

1. **`tools_called` 记录的是"尝试"**（含失败调用）——harness 的 `ToolCallRecorder` 记的是 `call()` 发起过的工具名，`browser_open` 失败也会出现在列表里 → 它只能证明"链走到了这一步"，**不能证明"页面真的打开了"**。
2. **失败模板会回显用户输入**——失败文案原样带着任务里的 URL/城市名/任务原文：

| 场景 | 曾经的断言 | 失败运行为什么也能满足 |
|------|------------|------------------------|
| `t3_browser_open` | `contains_keyword: ["Example", "example.com"]` | 失败模板 `打开页面失败：https://example.com（…）` 原样回显小写域名 |
| `t3_browser_snapshot` | `contains_keyword: ["页面", "快照", "标题", "链接"]` | 失败模板 `还没有打开的页面：请先调用 browser_open(url)…` 含"页面" |
| `t1_weather_beijing` / `shanghai` | 只断言答案含城市名 | search 兜底的 `no results for query: '<任务原文>'` 回显任务原文（含城市名） |

再加上 **`partial`（只导航未观察）也 PASS**、**顺序完全未断言**——最坏情况：一条"全策略失败"或"只打开没观察"的运行，在报告里是全绿正例，**直接虚高后续真实数据**。

### 4.2 RED 证据：10 个用例红（可复现）

冻结的回归网先写测试：对每条**正例**场景构造"全策略失败"与 `partial` 两种**合成 outcome**（刻意复刻真实失败模板：错误消息回显任务原文、`tool_calls`=尝试过的工具、全败时 `stop_reason` 仍是 `completed`），要求**至少一项断言失败**。

- 当时的 RED 结果（SDD ledger 记账）：_合成全失败/partial outcome 让 5 条正例场景全绿（10 用例红）_。
- 本素材交付时复跑验证（只读：用 `db44335`（= `dddb99b^`，修复前）的 YAML + 当前守卫逻辑）：

```
# 复跑输出摘要（全绿场景 = 在两种合成 outcome 上都全部断言通过）
pre-fix all-green under synthetic outcome:
  t1_weather_beijing    all_failed / partial   [tier_is, tools_called, contains_keyword]
  t1_weather_shanghai   all_failed / partial   [tier_is, tools_called, contains_keyword]
  t3_browser_interaction all_failed / partial  [tools_called, contains_keyword]
  t3_browser_open       all_failed / partial   [tier_is, tools_called, contains_keyword]
  t3_browser_snapshot   all_failed / partial   [tools_called, contains_keyword]
total red cases: 10
```

### 4.3 修复三件套（对每条场景的断言做手术，不是放宽阈值）

1. **证据型关键词**：只用真实产物里才有的词——`t3_browser_*` 改用页面标题 `Example Domain`（失败文案里不出现）；通用词（"页面"/"链接"/小写域名）全部去掉。
2. **`degradation_absent`**：全策略失败时 `degradation_log` 非空必挂（graph 全败时 `stop_reason` 仍是 `completed`，单靠终止原因抓不住）。
3. **`stop_reason_is: completed`**：partial（只导航未观察）必挂。
   配套：`exact_sequence` 断言观察链顺序；`not_contains` 把失败模板词当补充防线。

### 4.4 合成回归网：23 个测试，进 CI（"禁止虚假通过"机械化）

- `test_synthetic_all_failed_outcome_fails_at_least_one_check[<11 条场景>]` + `test_synthetic_partial_outcome_fails_at_least_one_check[<同 11 条>]` + `test_synthetic_outcome_guard_covers_scenarios_and_exclusions_exist` = **23 个测试**（`uv run pytest tests/test_eval/test_scenarios.py --collect-only` 实测）。
- 失败路径场景显式豁免（它们断言的**就是**"失败必须诚实"）：`t1_weather_not_found` / `t2_read_page_failure` / `degrade_browser_unavailable`；**豁免清单由测试钉住**（写错 id = 场景悄悄逃出回归网）。
- 同一条红线也落在 schema 层：空 prompt / 空 checks / 空 keywords / 空 tools / 缺 `limit` 是加载错误或 FAIL，不是静默通过。

### 4.5 真图交叉验证（合成之外再钉一层）

修复做了一次"真实失败文案跑真图"的交叉验证（ledger 记账；探针 trace 保留在 `data/traces/probe-REAL-FAILURE-t3_browser_open.jsonl`、`probe-REAL-PARTIAL-t3_browser_open.jsonl`、`probe-old-kw.jsonl`）：旧断言在新失败运行上 PASS → 修后 FAIL。文章论点：**合成 outcome 可能写得不像真实失败，所以要拿真图的文案再验一次**——两层的成本是一次测试 + 三个探针。

### 4.6 教训与金句

- **红线**："虚假通过的断言比没有断言更糟"——没有断言你知道自己没测，虚假通过的断言让你以为测过了。
- 计划尾部的原始表述（`docs/superpowers/plans/2026-08-16-browsepilot-v2-p4.md`）：_"评测的价值在于'断言的可信度'——宁可 fragile 标记也不写虚假通过的断言，宁可 mock 门禁也不让 flaky 的真实模式阻塞交付。"_
- 连带产物：`t2_search_structural` 的措辞断言被移交（§1.4）；`field` 空声明从"静默通过"改成 FAIL；`Check` 补 `extra="forbid"`。

---

## 5. 最硬的素材（二）：**评测 → 修复闭环** —— `t1_weather_not_found` 抓出真实产品缺陷

> 出处：P4-T5 真实模式抽查（2×smoke + 3×probes）→ 修复 commit `25b1c76`（`feat(agent): disclose degraded-path provenance in answers`）→ 复验 `data/eval-reports/t5-fix.json`；场景 YAML 顶部注释保留了现象原文与判决。

### 5.1 场景意图：不存在的城市必须明确失败、不得编造温度

```yaml
id: t1_weather_not_found
prompt: "亚特兰蒂斯今天天气怎么样？"
checks:
  - type: not_contains        # 编造的温度值长这样（"亚特兰蒂斯今天 22°C"）
    keywords: ["°C"]
  - type: contains_keyword    # 诚实失败的措辞
    keywords: ["无法", "找不到", "没有", "抱歉", "未能"]
```

### 5.2 真实现象：5 次真实运行 4 次 FAIL

- 5 次真实运行（2×smoke + 3×probes，约 55s/次）：**4/5 FAIL**。可查的 FAIL 记录：`data/eval-reports/p4-t5-prefix.json`（19:24:58，git a6c8b08）明细 `keywords hit=[]`，耗时 23673.85 ms；唯一 PASS 的那次（`p4-t5.json`，19:31:08）是模型**恰好**如实拒答。
- 失败运行里模型给出的答案原文（场景 YAML 注释引用）：

> "今天三亚亚特兰蒂斯水世界天气：气温26～31℃，有小阵雨…" / "…31度，有雨，湿度76%"

### 5.3 定位链（三步，全部有代码/state 证据）

1. 任务"亚特兰蒂斯今天天气怎么样？" → 路由 tier1/weather（规则命中）；
2. `weather` 工具返回 `city_not_found` → **设计内的 search 兜底**（`graph.py::execute`：降级到 search 时直接用任务原文当 query）；
3. search 搜到**同名的真实实体**"三亚亚特兰蒂斯水世界" → 模型**隐式换了实体**，把水世界的天气当作"亚特兰蒂斯"的答案答出去——**答案不声明来源**，用户无从知道这是兜底结果。

判决：**违 spec §2「带说明的降级回答 / 禁止答非所问」**，是**产品缺陷**，不是"断言过严"（评测侧的红线：不把措辞改宽——那是把真实缺陷洗成 PASS）。

### 5.4 修复（`25b1c76`）：把"降级来源"交给答案模型

- `_degraded_provenance(state)`：**降级后仍拿到数据**时，把 `degradation_log`（按 `(strategy, error)` 去重）作为 `degraded_from` 注入 answer payload；**全败路径不注入**（那条路径的 answer 分支自己逐条渲染原因，再交一份就是把同一件事说两遍）。
- `_DEGRADED_ANSWER_RULE`（只在 payload 里真的有 `degraded_from` 时拼进 system，不稀释正常回答）三条要求：①先说明原始尝试失败了、失败原因是什么（"未能找到…"/"无法获取…"，不得略过）；②再说明这份数据来自哪条兜底路径；③**不得把兜底结果当作原问题的答案**——不得用兜底结果里的实体替换原问题里的实体，也不得把该实体的具体数值当作原问题的答案。

### 5.5 复验（PASS + 答案原文）

- `data/eval-reports/t5-fix.json`（19:40:28，`environment.git_commit=25b1c76`，`--tag smoke`）：**executed=6，passed=6，failed=0，exit 0**（修复前同口径 `p4-t5-prefix.json` 是 passed=4/failed=2）。
- 答案原文（SDD ledger 记账的复验输出，**答案全文未落盘**——报告不存 answer 全文，见 §5.6）：

> "未能找到名为'亚特兰蒂斯'的城市天气…这份数据来自兜底搜索路径…请注意：以上结果均与'亚特兰蒂斯'作为城市无关，不能作为该问题的天气答案。"

- trace 侧证据：`data/traces/t5fix-real-verbatim.jsonl`（2026-09-11T11:40:34Z）——同一条 execute span 里 `weather` error（3.3s）→ `search` success（15.2s），execute 总 18.5s，answer 1.56s。**"同一条 span 里 error → success"就是降级链的可视化签名**（与文章 5 素材的 P2 demo-a / P3 tier3 同构）。

### 5.6 残留（诚实记账）

- 兜底成功后 `stop_reason` 仍是 `completed`（spec 只要求"带说明"，未改成 degraded 语义）；
- 报告不存 answer 全文 → 案例的答案原文取证只能依赖 ledger 记账与探针日志（写文章引用时标注"ledger 记账原文"）；
- 这条修复越出了"只改评测"的边界（改的是 `src/browsepilot/agent/graph.py`）——**评测的价值正在于此：它把产品缺陷变成了可复现的证据**。

---

## 6. 失败归因的方法论（产品缺陷 vs 评测/环境问题）

### 6.1 §7.5 的 A/B 分类（3 轮 acceptance 的完整归因）

**A. 评测 / 环境问题（2 条，均非产品缺陷）**

| # | 场景 | 表现（逐字） | 复现证据 | 归因 |
|---|------|--------------|----------|------|
| A1 | `t2_search_content`（fragile，tier1） | 3/3 FAIL，明细 `keywords hit=[]`；运行本身 `stop_reason=completed`、`tools=['search']`、无降级日志 | 单场景复跑探针答案原文："抱歉，根据现有数据无法找到 Python 3.13 的正式发布时间。搜索结果中没有相关信息。"（`tokens=432`，`chat_calls=1`/`model_calls=2`）；ddgs 结果全部无关（jieba 分词教程 / Python 3.10 发布说明 / OSCHINA 标签页 / Mac 安装文档 / Debian 博客） | **依赖内容漂移 + 产品诚实拒编（设计行为）**：断言依赖 ddgs 返回内容 → 断言不可能满足；不是回归 |
| A2 | `hitl_login_detect`（非 fragile，tier3） | 全量 1 轮（headless）与 `--tag hitl` 专跑（headful）**都是 FAIL**，明细同为 `pending_human_action=False`，耗时 22729.3 / 29033.35 ms | 探针答案原文："抱歉，这个任务暂时无法完成：browser_open: 打开页面失败：https://github.com/login（TimeoutError: Page.goto: Timeout 30000ms exceeded …）；browser_snapshot: 还没有打开的页面…"；`degradation_log` = `browser_open:timeout` + `browser_snapshot:no_open_page` → `all_strategies_failed` | **环境问题（场景站点不可达）**：本机到 github.com/login 的 30s 超时 → 页面从未加载 → 登录墙检测不可能触发；headful/headless 一致 → 排除"headless 才测不出"。**HITL 的 `needs_human` 链路结论 = 未获验证（既不是通过，也不是已确认缺陷）** |

**B. 产品缺陷（acceptance 3 轮内 0 条）**：非 fragile 失败 = 0；且失败路径有正向证据——`t2_read_page_failure` 3/3 PASS（DNS 失败 → 明确报错、无 Traceback、不编造）、`t3_browser_open/snapshot` 3/3 PASS、`multi_turn_context` 3/3 PASS（第二轮"那明天呢？"的最终答案含"北京" → 历史注入生效）。

### 6.2 三条可复制的方法（文章的方法论段落）

1. **先问"这条 FAIL 断言的是什么"**：产品结构（工具链/终止原因/降级）？外部内容（ddgs 结果）？环境可达性（站点/浏览器/凭据）？三类对应三种处理（修产品 / 标 fragile / 记环境阻塞），**不能混谈**。
2. **环境问题要有独立于断言的证据**：A1 用单场景复跑探针（拿到 ddgs 结果原文与 answer 原文）；A2 用 headful/headless 对照 + `goto` 超时原文。没有探针原文的"环境问题"就是甩锅。
3. **未验证 ≠ 通过**：HITL 链路如实写"未验证"，并把它算在"产品失败"计数里（全量 1 轮 exit=1 的来源），同时在报告环境行/探针区分开列——**数字照实，归因分开**。

### 6.3 一句话

"宁可 fragile 标记，也不写虚假通过的断言；宁可标'未验证'，也不把环境问题洗成产品通过。"

---

## 7. p0-search 漂移的三次演进：P1 通过 → P2/P4 漂移 → 结构化解耦

> 这是"断言与动态内容解耦"（P4-D6）最完整的跨阶段证据链，三个阶段的原文都可查。

| 阶段 | 断言形态 | 结果 | 当时的原文/证据 |
|------|----------|------|-----------------|
| **P1** | `contains_keyword: ["2024"]` | ✅ PASS | `docs/v2-p1-report.md` §二：`p0-search ✅ PASS keywords hit=['2024']`，search 工具 4.22s + 回答 0.96s ≈ 5.2s；§六 清单"Tier1 搜索：ddgs + tenacity 重试，p0-search 评测 PASS" |
| **P2** | 同一条断言 | ❌ FAIL（真实模式 2/3） | `docs/v2-p2-report.md` 偏差 D1：ddgs 结果漂移（无 "2024" 关键字来源；返回 Python 3.8/3.10 相关页面），模型诚实不捏造 → 检查项落空；"**非代码回归**；mock 3/3 为主证据；建议 P4 加固评测" |
| **P4-T2/T5** | 拆成结构版 + 内容版 | 结构版 ✅ 3/3 轮 PASS；内容版 ❌ 3/3 轮 FAIL（fragile，不 gate） | `eval/scenarios/t2_search_structural.yaml`、`t2_search_content.yaml` 顶部注释；`docs/v2-eval.md` §3.1/§7.2 |
| **P4-T5 追加** | 结构版连措辞也不依赖了 | ✅ | 真实抽查发现结构版残留的 `contains_keyword ["http"]` 稳定 FAIL（措辞依赖）→ 删除并移交内容版；`test_structural_search_scenario_does_not_assert_on_answer_wording` 钉住 |

**文章论点**：
- 同一条断言在 P1 是"正确"的（当时数据恰好满足），在 P2 变成"红色噪音"——**依赖外部内容的断言，它的绿色不代表产品对，红色也不代表产品错**；
- 解耦的正确姿势不是"删断言"，而是**按依赖把断言分层**：可 gate 的结构面（工具被调用 / 无降级 / 正常完成）+ 只能记录的 fragile 内容面；
- 内容面的"可复现"**无法用断言保证**，只能如实记录（`fragile` 是承认，不是豁免）——换网络出口/换时点的结论都可能不同（`docs/v2-eval.md` §8.3）。

---

## 8. 代码 / 产物位置索引（供引用）

| 内容 | 位置 |
|------|------|
| 断言引擎（10 类型，纯函数） | `eval/checks.py::run_check`；兼容层 `eval/run_eval.py::check_outcome` |
| 场景 schema（Pydantic v2，`extra="forbid"`） | `eval/scenarios.py`（`Scenario` / `Check` / `KNOWN_TAGS` / `load_scenarios`） |
| 场景集（16 文件） | `eval/scenarios/*.yaml`（逐文件顶部注释含每次加固的判决） |
| 真实模式 harness（注入/隔离/采集） | `eval/harness.py`（`RealDeps` / `ToolCallRecorder` / `UsageAccumulator` / `UsageTrackingProvider` / `run_scenario` / `pool_lifecycle`） |
| 统计 + 双格式报告 | `eval/report.py`（`ScenarioResult` / `aggregate` / `write_json` / `write_markdown`） |
| CLI（`--mock/--tag/--json/--md/--repeat`） | `eval/run_eval.py::main` |
| 门禁脚本 / CI | `eval/gate.sh`；`.github/workflows/ci.yml` |
| 回归网（合成成功/失败 outcome 守卫） | `tests/test_eval/test_scenarios.py`（`_synthetic_all_failed` / `_synthetic_partial` / `_assert_not_all_green` / 23 个守卫用例） |
| 检查引擎单测 / harness 单测 / 报告单测 | `tests/test_eval/test_checks.py` / `test_harness.py` / `test_report.py` |
| 降级来源注入（案例 2 的修复） | `src/browsepilot/agent/graph.py::_degraded_provenance`、`_DEGRADED_ANSWER_RULE`、`_answer_payload` |
| 断言加固（案例 1 的修复） | commit `dddb99b`；`git show db44335:eval/scenarios/t3_browser_open.yaml`（修复前断言） |
| 结构化解耦 / p0 归档 | commit `61e1b98`；`tests/test_eval/test_scenarios.py::_P0_SUCCEEDED_BY` |
| 3 轮量化产物（gitignored） | `data/eval-reports/p4-t7.json|md`、`p4-t7-run.log`；复现探针 `t7-probe-t2searchcontent.txt`、`t7-probe-hitl.txt` |
| 案例 2 的运行/复验产物 | `data/eval-reports/p4-t5-prefix.json`（FAIL）、`p4-t5.json`（1 次 PASS）、`t5-fix.json`（修复后 6/6）；trace `data/traces/t5fix-real-verbatim.jsonl` |
| 评测体系文档（正式交付物） | `docs/v2-eval.md`（§3 哲学 / §4 场景规范 / §5 检查类型 / §6 运行指南 / §7 量化结果 / §8 已知限制） |
| P4 交付报告 | `docs/v2-p4-report.md` |

## 9. 开放问题（供文章讨论 / 已记账 → P5）

1. **`tokens_below` / `latency_below` 有引擎、无场景声明**——预算门禁还没进场景集；token 是 chat-only 下限，直接当硬预算会误判（当前只在单测里压过）。
2. **cost 未采集**（价格表超时）——成本维度目前是空白。
3. **HITL 未端到端验证**（需可达登录墙站点；`github.com/login` 30s 超时）——`needs_human` 链路既没证明通过也没确认缺陷。
4. **多轮架构缺口（已声明、未触发）**：`answer` 节点全败路径不注入 history；`classify`/参数提取只看任务原文（不看 history）→ 多轮场景**不**断言 `tools_called`（避免写一条必然抖动的断言）。
5. **tier3 交互链未接入**：`browser_type`/`browser_click` 已实现但 `graph._STRATEGY_CHAIN["browser_task"]` 未编排交互步 → `t3_browser_interaction` 只能 `enabled: false`。
6. **报告不存 answer 全文**：案例取证受限（本素材里的答案原文来自 ledger 记账与探针日志）。
7. **样本量边界**：3 轮 = 同一场景集重复跑；跨模型/跨网络稳定性**未采集**。
8. **mock 与 real 的能力差**：fake deps 让 mock 能稳定构造"没有浏览器"，但 mock 数字不能用来谈模型质量。

## 10. 一句话素材（供作者选用）

- "评测的价值在于断言的可信度——**虚假通过的断言比没有断言更糟**：没有断言你知道自己没测，虚假通过的断言让你以为测过了。"
- "t3 场景曾经能在'全失败'的运行上 PASS：`tools_called` 记的是尝试，失败文案里有'页面'两个字和用户输入的 URL。"
- "把 flaky 接进必过门禁，等于把门禁变成噪声源——所以 CI 只跑 mock，真实模式留给验收。"
- "同一条断言在 P1 是绿的，在 P2 是红的，在 P4 被拆成一层可 gate 的结构断言和一层如实记录的 fragile 内容断言。"
- "评测不只打分，它还抓 bug：'亚特兰蒂斯'的天气答成了三亚水世界的天气，5 次真实运行 4 次；修复后答案明确说'未能找到…不能作为该问题的天气答案'。"
- "token 是 chat-only 下限（30/63 次调用有计量），cost 未采集——读评测报告先读限制章节，再读成功率的数字。"
- "3 轮成功率零方差（stdev=0）、flaky 0 条——稳定的失败集合也可以归因，这和稳定的通过集合一样值钱。"
- "HITL 链路的结论是'未验证'，既不是通过也不是缺陷——把环境阻塞如实写进报告，是评测体系的一部分。"
