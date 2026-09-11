# BrowsePilot V2 评测体系

> 本文档在 P4 阶段完成（T8）：评测哲学（§3）· 场景规范（§4）· 检查类型（§5）· 运行指南（§6）· 3 轮真实数据与量化结果（§7）· 已知限制（§8）。
>
> **P5 刷新（2026-09-11，P5 Task 4）**：测试数基线更新到 **810**（§2/§6.1/§6.5）· P5-T0 的两处口径修正（§3.4/§5.1/§5.2/§5.3/§5.4：覆盖度分母不因"没有数字"而消失；`tier_is`/`stop_reason_is` 缺 `expected` → FAIL）· 新增交叉引用与历史读数注。
>
> **相关文档**：架构 `docs/v2-architecture.md` · 部署与环境变量全表 `docs/v2-deploy.md` · 性能基线 `docs/v2-performance.md`。

## 1. 环境与运行前提

| 项 | 值 | 说明 |
|----|----|------|
| Python | 3.14（`uv run`） | `uv run python -m eval.run_eval` |
| 模型 key | `BP_MODEL_API_KEY`（`Settings` 只读 `BP_` 前缀；v1 的 `OPENAI_API_KEY` 不会被自动读取，须显式映射，见 §8.6） | 真实模式必需；仅进程内，不入库 |
| 模型 id | `BP_DEFAULT_MODEL=deepseek/deepseek-chat` | 真实运行必钉：思考模型拒绝 forced tool_choice → structured 调用报错（§6.3）；`src/browsepilot/app/config.py` 的缺省值即此模型 |
| 浏览器 channel | `BP_BROWSER_CHANNEL=msedge` | 本机 Playwright 1.59 需 chromium rev 1217，缓存为 1208 → 默认启动失败；msedge channel 可用（或 `uv run playwright install chromium`） |
| HITL 场景 | `BP_BROWSER_HEADLESS=false` | 人工介入需可见浏览器窗口 |
| 导航策略 | `BP_NAVIGATION_POLICY=allow_https`（默认） | allowlist 模式见 `allowed_domains` |

## 2. P4 启动基线（2026-09-10 锁定）

| 指标 | 值 | 来源 |
|------|----|------|
| pytest | **637 passed** | P3 收口修复 `ac3286d` 后全量 |
| ruff / mypy | clean | 同上 |
| eval mock | **3/3 PASS** | 同上 |
| eval real | **2/3 PASS**（p0-search FAIL） | P3 交付报告 `dc98e19`；根因 = ddgs 结果漂移 + 模型诚实拒编（P2 D1 同因，P4-T5 结构性加固） |
| 场景数 | 3（p0-weather / p0-search / p0-read-page） | `eval/scenarios/` |
| 检查类型 | 3（field / contains_keyword / 未知→失败） | `eval/run_eval.py:check_outcome` |

> 本节是 P4 **启动时**的快照（对照用）。P4 收口后的场景集（16 个文件）与检查类型（10 种）见 §4/§5，运行口径见 §6。
>
> **P5 刷新（2026-09-11，实测口径）**：本节数字保持原样（历史快照）。P5 阶段测试数演进：**801**（P5-T1 删除 v1 测试后：该读数**达成于 `07bc3bd`**（删 v1 代码），在 `3d51612` 时点实测）**→ 810**（P5-T3 新增 `tests/test_eval/test_bench.py` 9 条后，HEAD `7304bee`；`uv run pytest tests/ -q` = `810 passed`，`--collect-only` 同样 810）。P5 的性能读数另见 `docs/v2-performance.md`（同一 HEAD）。

**P3 → P4 交接要点**（来自 P3 final review + 收口修复）：
- tier3 策略链 = `[browser_open, browser_snapshot]`；type/click 未入链（场景需标 `enabled: false`）
- 无导航的观察 / 只导航未观察 → `stop_reason="partial"`；全策略失败仍报 `completed`（既有行为）
- 真实模式 harness 已注入 BrowserPool + 逐场景 `aclose()` 隔离（`ac3286d`）
- answer 节点已读 `human_response`（resume 后不再复述"请手动登录"）

## 3. 评测哲学（T8 完成）

> 计划尾部原则：**评测的价值在于"断言的可信度"** —— 宁可 fragile 标记也不写虚假通过的断言，宁可 mock 门禁也不让 flaky 的真实模式阻塞交付。以下是这条原则在代码里的四个落点。

### 3.1 断言与动态内容解耦（P4-D6）

场景断言只允许依赖两类东西：**产品结构**（工具链、路由层级、终止原因、降级记录、数据字段、用量/耗时预算）与**数据断言**（如天气的 `field city` / `temperature_c`）；不允许依赖外部动态内容的**具体值**（搜索结果条目、页面正文、模型措辞）。断言一旦依赖 ddgs 返回内容，"外部数据漂移"就会被读成"代码回归"。

同一个 p0-search 场景的三次形态演进（第一手记录在 `eval/scenarios/p0_search.yaml`、`t2_search_content.yaml` 的顶部注释）：

| 阶段 | 形态 | 结果 | 证据 |
|------|------|------|------|
| P1 | `p0-search` 内容断言 `contains_keyword: ["2024"]` | PASS：当时的 ddgs 结果含 "2024" | `docs/v2-p1-report.md` §二 |
| P2 | 同一条断言 | 真实模式 FAIL：ddgs 本次返回与问题无关（Python 3.8/3.10 页面）→ 模型如实拒答（`keywords hit=[]`），**非代码回归** | `docs/v2-p2-report.md` 偏差 D1 |
| P4-T5 | 拆成两条：`t2_search_structural`（结构断言 `tools_called: [search]` + `degradation_absent` + `stop_reason_is: completed`）与 `t2_search_content`（内容断言，`fragile: true`） | 结构版 3/3 PASS（§7.2）；内容版如实 FAIL，但**不计入门禁 exit code** | `eval/scenarios/t2_search_*.yaml`；§7.2 / §7.5 A1 |

`fragile` 的语义（`eval/run_eval.py`）：失败照实打 `[FAIL] … (fragile=true — 不计入门禁 exit code)`，进 `fragile_failed` 单列计数，报告里单独统计 —— 它是**"断言对环境的依赖被显式承认"**，不是"放宽通过标准"。解耦的底线是**删断言前先移交**：结构版删掉的原断言 `contains_keyword ["http"]`（"答案里出现结果链接"，真实模式稳定 FAIL 的措辞依赖）被移交给内容版（fragile），且由 `test_structural_search_scenario_does_not_assert_on_answer_wording` 钉住"结构版不得写措辞断言"。

### 3.2 禁止虚假通过（T2-fix 红线）

失败模板会回显用户输入，而 `tools_called` 记录的是**尝试**（含失败调用）——两条叠加，会让"只断言工具名 + 关键词"的正例场景在**全策略失败**的运行上全绿。真实案例（T1+T2 审查 C1 发现；修复记录在各 YAML 顶部注释）：

| 场景 | 曾经的断言 | 失败运行为什么也能满足 |
|------|------------|------------------------|
| `t3_browser_open` | `contains_keyword: ["Example", "example.com"]` | 失败模板 `打开页面失败：https://example.com（…）` 原样回显小写域名 |
| `t3_browser_snapshot` | `contains_keyword: ["页面", "快照", "标题", "链接"]` | 失败模板 `还没有打开的页面：请先调用 browser_open(url)…` 含"页面" |
| `t1_weather_beijing` / `shanghai` | 只断言答案含城市名 | search 兜底的 `no results for query: '<任务原文>'` 回显任务原文（含城市名） |

修复手法（三件套）：① **证据型关键词** —— 只用真实产物里才有的词（`t3_browser_*` 改用页面标题 `Example Domain`，失败文案里不出现）；② **`degradation_absent`** —— 全策略失败时 `degradation_log` 非空必挂（graph 全败时 `stop_reason` 仍是 `completed`，单靠终止原因抓不住）；③ **`stop_reason_is: completed`** —— partial（只导航未观察）时挂。

**回归网是可执行、进 CI 的**（`tests/test_eval/test_scenarios.py`）：对每条正例场景合成两种"对断言最不利"的 outcome —— `_synthetic_all_failed`（忠实复刻失败模板：错误消息回显任务原文、`tool_calls` = 尝试过的工具、全败时 `stop_reason="completed"`）与 `_synthetic_partial`（`partial` / `partial_reason` 与 graph 同形）—— 要求**至少一项断言失败**：

- `test_synthetic_all_failed_outcome_fails_at_least_one_check[<正例场景>]`
- `test_synthetic_partial_outcome_fails_at_least_one_check[<正例场景>]`

失败路径场景显式豁免（它们断言的**就是**"失败必须诚实"）：`t1_weather_not_found` / `t2_read_page_failure` / `degrade_browser_unavailable`；豁免清单由 `test_synthetic_outcome_guard_covers_scenarios_and_exclusions_exist` 钉住（写错 id = 场景悄悄逃出回归网）。同一条红线也落在 schema 层（§4/§5）：空 prompt / 空 checks / 空 keywords / 空 tools / 缺 `limit` 是**加载错误或 FAIL**，不是静默通过。

### 3.3 分层门禁（P4-D2 / P4-D8）

tag（§4.3）决定场景在哪个面里是"必过"，`--tag` 取任一匹配；门禁实现 = `eval/gate.sh` 与 `.github/workflows/ci.yml`：

| 运行面 | 内容 | 谁跑 |
|--------|------|------|
| mock（CI 必过） | `uv run pytest tests/ -q` → `ruff` → `mypy` → `uv run python -m eval.run_eval --mock` | `eval/gate.sh`（本地一键）＝ CI 同口径，**无网络 / 无 key / 无浏览器** |
| real（手动 / 验收） | `--tag acceptance --repeat N`（需 `BP_MODEL_API_KEY` + 浏览器 channel） | 人工；**绝不进 CI**（CI 无网络无 key，且真实结果受 ddgs 漂移影响会 flaky） |
| fragile 场景 | 失败照实 FAIL、单列 `fragile_failed` | 任何面都**不改变 exit code** |

判定规则（`eval/run_eval.py`）：

- `mode` 不匹配本次运行 → `[SKIP]`，不计 pass/fail、不进 exit code —— "跑不了"（mock 没有浏览器）不是"跑失败了"，环境不匹配不能污染成功率；
- `enabled: false` → `[DISABLED]`（列出但不跑）：P3 遗留的交互链用这个口子声明；
- `--tag` 选不中任何可运行场景 → **exit 1**（空跑全绿与"跑过且全绿"在日志上无法区分）；
- 显式专用 tag（`hitl-live`）**只在** `--tag hitl-live` 下被选中：不在门禁 tag 里，无 `--tag` 的默认全量跑也跳过它（打印跳过原因）—— 环境依赖的场景（站点不可达 → 必然 FAIL 且非 fragile）不得让全量 real 跑的 exit 1 由环境构成（P5-T0b 裁决 b；口径见 §4.3，事实见 §8.2）；
- exit code = **任一非 fragile 失败 → 1，否则 0**；场景抛异常计为失败且**不中断**后续场景（`run_all` 逐场景捕获，打 `[FAIL] … EXCEPTION …` 继续跑）。

### 3.4 fail-loud：缺数据 → FAIL，不是 0

"不知道"与"是 0"必须区分开 —— 报告里的每个数字都要能回答"它从哪来"：

- **断言层**：`tokens_below` / `latency_below` / `tools_called` 拿不到数据时给可诊断的 FAIL（detail 指明缺哪个注入字段），绝不按空值通过；`field` / `contains_keyword` 缺声明同样是 FAIL；
- **采集层（P5-T0 修正）**：harness 采不到 **token 数字**时 token 键缺席（不填 0），但快照**照发** —— `usage_source` / `chat_calls` / `model_calls` 是采集到的事实，分母不能因为"没有数字"而少算（无数字时未计量的 chat 记为 `estimated_calls`）；**一次模型调用都没有时**整条 `token_usage` 才缺席（`UsageAccumulator.snapshot()` 返回 `None`）。provider 只报 total 时 `input_tokens` / `output_tokens` 缺席（未知不是 0）；`cost_usd` 只在 > 0 时出现（0 多半是"算不出价"）；
- **聚合层**：`report.py` 里 `None` 不进样本、不填 0；样本数为 0 时 JSON 写 `null`、MD 写"—（未提供）"；每场景带 `measured_calls` / `model_calls`（token 覆盖度的诚实分母，§5.3）；
- **加载层**：未知 tag / 未知字段 / 未知 mode、空 checks、非正 `timeout_seconds`、重复 id、空目录 → `ScenarioLoadError`（报文件名）：一次笔误不得静默变成"少跑一条场景"或"少断言一条"。

### 3.5 交叉引用：P1 "天气 100% 可靠" 的证据链在此闭环（T8 Step 2）

P1 的结论是**单轮**读数（"p0-weather 评测 PASS（查天气 100% 可靠，不碰浏览器）"，`docs/v2-p1-report.md` §二/§六）；P4 把它换成"可复现的通过集合 + 方差证据"：

| 环节 | 内容 | 出处 |
|------|------|------|
| 承接场景 | `t1_weather_beijing`（`field city` + `field temperature_c (number)` + `tier_is` / `tools_called` / `degradation_absent` / `stop_reason_is` / `contains_keyword`）；`t1_weather_shanghai`（换城市，证明确实在解析任务）；`t1_weather_not_found`（负例：不存在的城市必须明确失败、不得编造温度） | `eval/scenarios/t1_weather_*.yaml` |
| 断言迁移 | 原 `p0-weather` 的 `field city` / `field temperature_c (kind: number)` 逐条迁入 `t1_weather_beijing`；`p0-weather` 文件保留（`enabled: false`）供回归对比 | `test_p0_assertion_semantics_have_enabled_successors`（缺承接者 = 存档变成丢断言） |
| 3 轮数据 | 三城场景各 3/3 PASS；逐场景耗时/token 记录在案 | §7.2 |
| 方差证据 | 成功率跨轮 stdev = 0（每轮都是同一组 9 PASS + 1 fragile FAIL）；P50 stdev 328.77 ms、token stdev 24.91（<1%）；flaky = **0 条** | §7.3 / §7.4 |

结论：P1 的"100% 可靠"在 P4 有了**可复现的通过集合**（3 轮同因同果）与**波动读数**（P95 的抖动来自依赖网络，不是天气链路）；"单轮结论缺方差证据"这个缺口由 §7 的 3 轮数据补齐。

## 4. 场景规范（YAML schema）（T8 完成）

> 实现：`eval/scenarios.py`（Pydantic v2，`extra="forbid"`）。一个场景 = 一次 `run_task`（有 `follow_up` 时多轮）+ 一组断言 + 运行选择元数据。字段/类型/默认值逐字取自 `Scenario.model_fields` / `Check.model_fields`；场景文件位于 `eval/scenarios/*.yaml`（按文件名字典序加载）。

### 4.1 场景字段（`Scenario`）

| 字段 | 类型 | 默认 | 语义与校验 |
|------|------|------|------------|
| `id` | `str` | 必填 | 场景唯一标识；strip 后非空；全目录唯一（重复 → 加载报错并指出两个文件） |
| `name` | `str` | 必填 | 人读名称（日志的 `[PASS]` / `[FAIL]` / `[DISABLED]` 行使用） |
| `prompt` | `str` | 必填 | 第一轮任务原文；strip 后非空 |
| `tier` | `Literal["tier1", "tier2", "tier3"]` | `"tier1"` | **路由的实际层级**（search 规则命中 → `tier1`，即便文件名叫 `t2_*`）；拼错（`tier4`）在加载期报错 |
| `tags` | `list[str]` | `["acceptance"]` | 只能取 `KNOWN_TAGS`（§4.3）；未知 tag 报错并列出合法集；空列表等价缺省 |
| `mode` | `Literal["mock", "real", "both"]` | `"both"` | 这条场景在哪个模式下有意义；与本次运行不匹配 → `[SKIP]`（不计 pass/fail） |
| `checks` | `list[Check]` | `[]`，但**必须非空** | 断言列表（§4.2 / §5）；空 checks = 必然通过 → 加载错误 |
| `timeout_seconds` | `float`（`gt=0`） | `120.0` | 每轮的墙钟预算（多轮场景每轮一次）；超时抛 `TimeoutError`（消息带 id / 轮次 / 预算）；0 或负数报错 |
| `fragile` | `bool` | `False` | 断言依赖外部动态内容：失败照 FAIL、单列 `fragile_failed`，**不改变 exit code**（§3.1） |
| `enabled` | `bool` | `True` | `False` → `[DISABLED]`：列出、不跑、不进统计（P3 遗留场景用） |
| `follow_up` | `list[str]` | `[]` | 第 2..n 轮 prompt；`checks` 评估**最后一轮**的 outcome；`token_usage` / `duration_ms` / `tool_calls` 是**整场景**（各轮累加）口径 |

### 4.2 Check 字段（`Check`）

| 字段 | 类型 | 默认 | 用于 / 语义 |
|------|------|------|-------------|
| `type` | `str` | 必填 | 10 种检查类型之一（§5）；未知类型运行时 FAIL（`unknown check type …`，不抛异常） |
| `field` | `str`（可空） | `None` | `field`：要断言的数据键（`result_data[field]`） |
| `expected` | `str`（可空） | `None` | `field` / `tier_is` / `stop_reason_is` 的期望值 |
| `kind` | `str`（可空） | `None` | `field` 的 `number`：只断言"是数值"（`bool` 不算数值） |
| `keywords` | `list[str]`（可空） | `None` | `contains_keyword` / `not_contains` 的关键词；同时是 `tools_called` 的 `tools` 别名 |
| `tools` | `list[str]`（可空） | `None` | `tools_called` 的期望工具名 |
| `mode` | `str`（可空） | `None`（=`contains`） | `tools_called` 的匹配模式：`contains` / `not_contains` / `exact_sequence`（有序子序列） |
| `limit` | `int`（可空） | `None` | `tokens_below` 的 token 预算 |
| `limit_ms` | `float`（可空） | `None` | `latency_below` 的毫秒预算 |

### 4.3 合法 tag 集（`KNOWN_TAGS`）

| tag | 语义（`eval/scenarios.py`） | 当前场景（16 文件口径） |
|-----|------------------------------|--------------------------|
| `smoke` | CI 必跑的确定性核心（mock 下可复现、不依赖外部内容漂移） | `t1_weather_beijing` / `t1_weather_shanghai` / `t1_weather_not_found` / `t2_read_page` / `t2_read_page_failure` / `t2_search_structural`（+ 归档的 `p0-weather`）—— 与 §7.6 冒烟运行 `passed=6` 一致 |
| `acceptance` | 阶段验收全量（**缺省值**：不写 tags 就是这条） | 除 `degrade_browser_unavailable` / `hitl_login_detect` 外的全部运行中场景 |
| `browser` | 需要真浏览器 | `t3_browser_open` / `t3_browser_snapshot` / `t3_browser_interaction` / `hitl_login_detect` |
| `hitl` | 需要真浏览器 + 人工介入（headful，`BP_BROWSER_HEADLESS=false`） | `hitl_login_detect` |
| `hitl-live` | 真人在环 + **可达登录墙**（P5-T0b 裁决 b）：**只在显式 `--tag hitl-live` 时被选中** —— 不在任何门禁 tag 里，也不进无 `--tag` 的默认全量跑（`run_eval.EXPLICIT_ONLY_TAGS`），跳过时打印原因 | `hitl_login_detect` |
| `degrade` | 降级行为（工具不可用时的诚实失败） | `degrade_browser_unavailable`（`mode: mock`："没有浏览器"只能在 mock 里稳定构造） |

> **`hitl-live` 为什么要单独一格**（P5-T0b，详见 §8.2）：`hitl_login_detect` **非 fragile**，但它依赖"可达的登录墙站点"——本机到 github.com/login 的 `goto` 30s 超时 → 在可预见的将来**必然 FAIL**。若它留在默认跑里，无 `--tag` 的 real 全量跑**永远** exit 1，而这个 1 由**环境**构成（不是产品缺陷）——发布验收信号里混进与产品无关的噪声。隔离后：`--tag acceptance` / `--tag smoke` 不含它（本来就不含）；无 `--tag` 的默认全量跑也跳过它（**打印原因**，不静默少跑一条）；要跑就显式 `--tag hitl-live`（=声明"这次站点可达 / 我接受它失败"）。换可达站点或本地 fixture 页后可以把它降回 `hitl`/`acceptance` 口径（P5+ 可选改进）。

### 4.4 校验与编写规则（fail-loud）

- **`extra="forbid"`（`Scenario` 与 `Check` 同）**：未知字段 —— YAML 里 `mdoe: real` 这类笔误、或 Check 里 `keyword:` 漏 s —— 一律报错。静默忽略 = 以错误的 mode/enabled 跑、或少断言一条，而日志上看不出来。
- **`tier` / `mode` 是 Literal**：拼错在加载期报错，不带进运行期。
- **`tags` 逐项校验**：未知 tag 报错并列出合法集合（`KNOWN_TAGS`）。
- **非空**：`id` / `prompt`（strip 后）、`checks`（否则"空场景 = 必然通过"）；`timeout_seconds` 必须 `> 0`。
- **唯一性与目录**：重复 `id` 报错并指出两个文件（同一场景跑两遍会悄悄污染成功率）；目录里一条 YAML 都没有 → 报错（空跑全绿没有意义）。所有加载错误都带文件名。
- **prompt 必须命中声明 tier 的规则**：`classify_by_rules(prompt).tier == tier`（回归网 `test_every_scenario_prompt_routes_to_its_declared_tier`，离线可跑）。tier3 提示词**不得**含 `http(s)://` 或 打开/访问/读/阅读/摘要（`read_page` 规则优先命中），要靠"浏览器 / 搜索框"等强触发词落 tier3。
- **断言的可信度**：正例场景必须能在"全策略失败"与"partial"合成 outcome 上挂至少一项（§3.2 回归网）；依赖外部内容的断言必须标 `fragile: true`，且结构场景不得写措辞断言（`t2_search_structural` 由测试钉住）。

### 4.5 完整示例（逐字取自 `eval/scenarios/t1_weather_beijing.yaml`）

```yaml
# Tier1 正例（P4-T2 审查 I2/I3 加固）：happy path 必须与失败/partial 可区分。
#
# 为什么光有 contains_keyword 不够：weather 失败后回退 search，search 也失败时的错误消息是
# `no results for query: '<任务原文>'` —— 城市名被原样回显，于是"答案含北京"在最彻底的
# 失败运行上也成立（T1+T2 审查 I2 的假通过）。
#
# 断言分工：
# - field city：数据断言（p0-weather 的先例）—— 只有 weather 工具真的成功才有这个字段；
#   代价：search 兜底"救回"任务时本条会失败 —— 对**正例**场景这是可接受的（兜底 = 主路径没走通）。
# - field temperature_c（kind: number）：P4-T5 从 p0-weather 存档时**迁移**过来的断言（"温度必须是
#   数值"，不是文本匹配）—— 存档 p0-* 不得等于丢断言：两句数据断言现在都归这条场景。
# - degradation_absent / stop_reason_is：策略链一次失败都不许有；partial 不得报 completed。
id: t1_weather_beijing
name: 北京天气（Tier1 正例：路由 + 工具 + 城市名）
prompt: "北京今天天气怎么样？"
tier: tier1
tags: [smoke, acceptance]
mode: both
checks:
  - type: tier_is
    expected: tier1
  - type: field
    field: city
    expected: 北京
  - type: field
    field: temperature_c
    kind: number  # P4-T5：随 p0-weather 存档迁入（温度必须是数值，不是文本匹配）
  - type: tools_called
    tools: [weather]
  - type: degradation_absent
  - type: stop_reason_is
    expected: completed
  # 保留：可读性断言（城市名出现在答案里）。失败运行上的假通过由上面 field /
  # degradation_absent 两条挡住 —— 回归网见 tests/test_eval/test_scenarios.py 的
  # "合成失败 outcome 不得让正例场景全绿"。
  - type: contains_keyword
    keywords: ["北京"]
```

### 4.6 当前场景集（16 个文件，`eval/scenarios/`）

> 文件名 = `<id>.yaml`（三个归档场景例外：文件用下划线 `p0_weather.yaml` / `p0_search.yaml` / `p0_read_page.yaml`，id 用连字符）。下表逐字由 `load_scenarios()` 读出（按文件名字典序 = 加载顺序）。

| id | tier | mode | tags | 状态 | fragile | 断言类型 |
|----|------|------|------|------|---------|----------|
| `degrade_browser_unavailable` | tier3 | mock | degrade | 运行中 | — | `tier_is` / `tools_called` / `contains_keyword` / `not_contains` |
| `hitl_login_detect` | tier3 | real | hitl, browser, hitl-live | 运行中 | — | `needs_human` / `tools_called` |
| `multi_turn_context` | tier1 | real | acceptance | 运行中 | — | `contains_keyword` |
| `p0-read-page` | tier2 | both | acceptance | 归档 | — | `contains_keyword` |
| `p0-search` | tier1 | both | acceptance | 归档 | ✓ | `contains_keyword` |
| `p0-weather` | tier1 | both | smoke, acceptance | 归档 | — | `field` / `field` |
| `t1_weather_beijing` | tier1 | both | smoke, acceptance | 运行中 | — | `tier_is` / `field` / `field` / `tools_called` / `degradation_absent` / `stop_reason_is` / `contains_keyword` |
| `t1_weather_not_found` | tier1 | both | smoke, acceptance | 运行中 | — | `not_contains` / `contains_keyword` |
| `t1_weather_shanghai` | tier1 | both | smoke, acceptance | 运行中 | — | `tier_is` / `field` / `tools_called` / `degradation_absent` / `stop_reason_is` / `contains_keyword` |
| `t2_read_page` | tier2 | both | smoke, acceptance | 运行中 | — | `tools_called` / `contains_keyword` / `degradation_absent` / `stop_reason_is` |
| `t2_read_page_failure` | tier2 | both | smoke, acceptance | 运行中 | — | `not_contains` / `contains_keyword` |
| `t2_search_content` | tier1 | both | acceptance | 运行中 | ✓ | `contains_keyword` |
| `t2_search_structural` | tier1 | both | smoke, acceptance | 运行中 | — | `tools_called` / `degradation_absent` / `stop_reason_is` |
| `t3_browser_interaction` | tier3 | real | browser, acceptance | 待启用 | — | `tools_called` / `contains_keyword` / `degradation_absent` |
| `t3_browser_open` | tier3 | real | browser, acceptance | 运行中 | — | `tier_is` / `tools_called` / `contains_keyword` / `degradation_absent` / `stop_reason_is` |
| `t3_browser_snapshot` | tier3 | real | browser, acceptance | 运行中 | — | `tools_called` / `contains_keyword` / `stop_reason_is` / `degradation_absent` / `not_contains` |

计数口径：16 = **8 可 mock 执行** + 4 mock 下 SKIP（real-only：tier3 两条 + HITL + 多轮）+ 4 停用（3 归档 + 1 待启用）。`--mock` 的日志形状与 §6.1 一致（`passed=8 skipped=4 disabled=4`）。

## 5. 检查类型表（T8 完成）

> 实现：`eval/checks.py` 的 `run_check(outcome, check) -> CheckResult(passed, detail, kind)` —— **纯函数、无 I/O**（不看时钟、不读文件、不发网络），数据全部来自传进来的 outcome；未知类型 / 未知 mode → `passed=False` + 明确 detail，不抛异常（一个笔误不该炸掉整轮评测）。`eval/run_eval.py:check_outcome` 是保留 tuple 形状的兼容薄适配层（既有调用方式不变）。

### 5.1 10 种类型

| `type` | 语义 | 字段 | 数据来源 | 缺数据 / 空声明（fail-loud） |
|--------|------|------|----------|-------------------------------|
| `field` | `result_data[field] == expected`；`kind: number` 只断言"是数值"（`bool` 不算数值） | `field` / `expected` / `kind` | `outcome["result_data"]` | 缺 `field` → FAIL；非 number 时缺 `expected` → FAIL；值不等（含字段缺席 = `None`）→ FAIL |
| `contains_keyword` | `final_answer` 含**任一** keyword | `keywords` | `outcome["final_answer"]` | 缺 `keywords` → FAIL；answer 缺席/非字符串按空串 → FAIL |
| `not_contains` | `final_answer` 不含**任何** keyword | `keywords` | `outcome["final_answer"]` | 缺 `keywords` → FAIL；answer 缺席按空串 → 通过（边界见 §5.4） |
| `tools_called` | 工具名断言：`contains`（默认，全出现）/ `not_contains` / `exact_sequence`（有序子序列） | `tools`（`keywords` 是别名）/ `mode` | `execution_log[*]["tool"]`（v2 graph 不写）→ harness 注入 `outcome["tool_calls"]`（`call()` 的全部工具名，含失败；多轮累加） | 缺期望工具 → FAIL；未知 `mode` → FAIL；无调用记录且期望非空 → FAIL |
| `tier_is` | `outcome["tier"] == expected` | `expected` | run_task state（路由**实际**层级） | 缺 `expected` → FAIL（P5-T0 后空声明不再算通过）；不等 → FAIL |
| `stop_reason_is` | `outcome["stop_reason"] == expected` | `expected` | run_task state | 缺 `expected` → FAIL；不等 → FAIL（同 `tier_is`） |
| `tokens_below` | 总 token ≤ `limit` | `limit` | `token_usage.total_tokens` → `usage.total_tokens` → 扁平 `total_tokens`（当前只有 harness 注入） | 缺 `limit` → FAIL；无用量数据 → FAIL（"没数据不算通过"） |
| `latency_below` | 总耗时 ≤ `limit_ms` | `limit_ms` | `outcome["duration_ms"]`（整场景墙钟）→ 否则 `execution_log[*]["duration_ms"]` 之和（**工具耗时之和**，不含模型调用） | 缺 `limit_ms` → FAIL；无耗时数据 → FAIL |
| `degradation_absent` | `degradation_log` 为空 | 无 | run_task state（策略链每次失败的降级记录） | 有记录 → FAIL（detail 带条数 + 策略名）；字段缺席与空列表同判通过（正向断言"没有降级"） |
| `needs_human` | `pending_human_action` 为真（`bool()` 真值） | 无 | run_task state（HITL interrupt 置位） | `False` / 缺席 → FAIL（HITL 的稳定信号；interrupt 发生在 answer 之前，`final_answer` / `stop_reason` 尚空，不要断言它们） |

当前场景集实际用到上表中 **8 种**；`tokens_below` / `latency_below` 有实现与单测（`tests/test_eval/test_checks.py`）但**尚无场景声明**（预算门禁还没进场景集；token 是 chat-only 下限，直接当硬预算会误判，见 §8.1/§8.8）。

### 5.2 数据来源：run_task state vs harness 注入（`eval/harness.py`）

| outcome 键 | 来源 | 形状 / 说明 |
|-------------|------|-------------|
| `tier` / `stop_reason` / `final_answer` / `result_data` / `degradation_log` / `pending_human_action` | `run_task` 返回的 `AgentState` dict | 产品的真实状态；`AgentState.execution_log` 有字段但 v2 graph **从不写** → `tools_called` 不能依赖它 |
| `tool_calls` | harness `ToolCallRecorder.call` | 本次场景**尝试过**的全部工具名（含失败调用）；多轮 = 各轮累加；无调用 → `[]` |
| `duration_ms` | harness `time.perf_counter()` | 整场景（含全部轮次）墙钟毫秒 |
| `token_usage` | harness `UsageAccumulator.snapshot()` | 见 §5.3；**无 token 数字时仍发覆盖度键**（`usage_source` / `chat_calls` / `model_calls`，不填 0）；**一次模型调用都没有时整条缺席**（P5-T0 修正） |

### 5.3 chat-only 用量口径（token / cost 的下界）

- 快照键（自描述，P4-T3 审查 I2；覆盖度口径按 P5-T0 修正）：**只要发生过模型调用就发**的覆盖度键 —— `usage_source="chat_only"`（这组数字只覆盖 chat 调用；`chat_calls=0` 时同时说明"没有 chat 调用可计量"）、`chat_calls`（有 usage 面的调用数）、`model_calls`（实际发生的全部模型调用 = chat + structured + stream，**诚实分母**）；token 数字键**没有可用数字时一律不出现** —— `total_tokens`（优先 provider 报的 total，缺失时退化为 input+output）、`input_tokens` / `output_tokens`（仅当两者之和 > 0）；`estimated_calls`（chat 里没报上量的次数；无数字时 = `chat_calls`）；`cost_usd`（仅当 > 0）。
- **为什么只有 chat**：`structured` 返回 schema 实例（litellm 的 usage 在重试循环里被丢）、`stream` 按 port 只 yield 字符串 —— 两个口都没有 usage 面。`UsageTrackingProvider` 只加一层"记账"（返回值原样透传），不改任何语义。
- 因此 token / 成本是**下限**：T7 三轮样本池 `measured_calls=30 / model_calls=63`（47.6%），`total_tokens=10191` 是下限而不是"总消耗"（§7.7）；报告每条结果行带 `measured_calls` / `model_calls`，MD 汇总读作"N/M 调用有计量"。
- 没有 token 数字 → token 键缺席（快照仍在，覆盖度分母不少算）→ `tokens_below` FAIL；报告写 `null` / "—（未提供）"（§3.4）。**一次模型调用都没有时**整条 `token_usage` 才缺席（例：全败模板答案路径不调模型 —— P5-T0 修正）。

### 5.4 一致性边界（逐条核对 `checks.py` 得到的精确行为）

- `tier_is` / `stop_reason_is` 缺 `expected` → **FAIL**（P5-T0 加的守卫：`expected` 缺席且 outcome 无该键时 `None == None` 不再判通过 —— 空声明不得算通过，与 `field` 同一条原则）。当前 16 个文件全部显式声明 `expected`（已核对）；新增场景必须写 `expected`。
- `not_contains` 在 `final_answer` 缺席/非字符串时按空串处理 → **通过**（没有可匹配的文本）；所以"不得编造 / 不得抛栈"类断言应与"必须有数据"的断言成对出现（当前配 `contains_keyword`）。
- `degradation_absent` 在 `degradation_log` 缺席时与空列表同判 → 通过（它是正向断言"没有降级"，不是"有数据才能判"）。
- `needs_human` 用 `bool()` 真值判定（`pending_human_action` 非假即通过）。
- `tools_called` 的 `exact_sequence` = **有序子序列**（期望工具按相对顺序出现即可，中间允许夹其他工具），不是全等序列。
- `latency_below` 拿不到 `outcome["duration_ms"]` 时退化为 `execution_log` 步骤耗时之和 —— 语义变成"**工具耗时之和**"（不含模型调用），阈值要按这个语义设（v2 graph 不写 `execution_log`，真实模式实际总是拿到端到端墙钟）。
- `field` 的 `kind` 只有 `number` 有特殊语义：其他值（含拼错）会走 `expected` 相等比较，且缺 `expected` 时直接 FAIL —— 不会静默通过，但新场景应写 `kind: number` 或省略 `kind`。

## 6. 运行指南（T8 完成）

> 本节的门禁与真实模式前置部分由 T6 提前填入（P4-D8）；T8 补全场景/检查细节与完整 runbook。

### 6.1 一键门禁 gate.sh（本地与 CI 同口径）

```bash
cd browsepilot && bash eval/gate.sh    # 或仓库根：bash browsepilot/eval/gate.sh
```

脚本自己定位项目目录；`set -euo pipefail`：任一步失败即停 + 非零退出；全绿时末尾打 `[gate] ALL PASS`。开头打印环境行（cwd / 分支 / HEAD 短号 / uv 版本 / 时间）——CI 或本机失败时，日志本身能回答"哪份代码、哪个环境"。

| 步骤 | 命令 | 说明 |
|------|------|------|
| 1 | `uv run pytest tests/ -q` | 全量单测 |
| 2 | `uv run ruff check src/ eval/ tests/` | lint |
| 3 | `uv run mypy src/browsepilot/` | 类型检查 |
| 4 | `uv run python -m eval.run_eval --mock` | 评测回归（FakeProvider/FakeExecutor：无网络 / 无 key / 无浏览器） |

mock 的日志形状（当前 16 条场景）：`passed=8 skipped=4 disabled=4`，exit 0。4 条 SKIP = tier3/HITL/多轮（Fake 没有浏览器，"跑不了"不是"跑失败了"）；4 条 DISABLED = 3 条 `p0_*` 归档场景 + `t3_browser_interaction`（交互链未入图）。数字随场景集演进，**判据始终是 exit code**。

**当前基线（P5-T3 后，2026-09-11，HEAD `7304bee`）**：步骤 1 = **810 passed**（步骤 2/3 = clean、步骤 4 = `passed=8 skipped=4 disabled=4`，exit 0）；数字演进与出处见 §2 的 P5 刷新注。环境变量（`BP_*` 全表）与启动方式见 `docs/v2-deploy.md`；分层与机制见 `docs/v2-architecture.md`。

### 6.2 CI 跑什么 / 不跑什么（P4-D8）

`.github/workflows/ci.yml`（触发：push `master`/`v2.0` + PR；全部 `working-directory: browsepilot`）：

```
Install（uv sync --all-extras --frozen）→ Lint → Types → Tests → Eval (mock)
```

- **跑**：mock 模式（`uv run python -m eval.run_eval --mock`）—— 只依赖 fake deps，无网络可复现。
- **不跑**：真实模式。CI 无网络、无模型 key、无浏览器；且真实结果受 ddgs 结果漂移影响会 flaky，把 flaky 接进必过门禁等于把门禁变成噪声源（P4-D8）。真实模式为手动/验收运行（可选定时任务），**绝不进 CI**。

> **CI 说明**：这套 workflow 至今**没有一次**真实的 Actions 运行记录（本地分支不推送）—— 引用 CI 时必须如实标注，见 §6.5。

### 6.3 真实模式运行前提

与 §1 的环境表同源；这里是跑 acceptance 的最小前提：

| 前提 | 值 | 为什么 |
|------|----|--------|
| 模型 | `BP_DEFAULT_MODEL=deepseek/deepseek-chat` | 思考模型（deepseek-flash / v4-pro）拒绝 forced tool_choice → structured 调用报错；显式钉住非思考模型 |
| 模型 key | `BP_MODEL_API_KEY` | 仅进程内环境变量（不入库）；留空时回退 LiteLLM 的 provider 环境变量 |
| 浏览器（tier3） | `BP_BROWSER_CHANNEL=msedge`，或 `uv run playwright install chromium` | 本机 Playwright 需 chromium rev 1217、缓存为 1208 → 默认启动失败；池起不来时报告环境行写 `unavailable (start failed)`，tier3 的失败**在报告里**归因到环境而非任务 |
| HITL 场景 | `BP_BROWSER_HEADLESS=false` | 人工介入需要可见窗口；且该场景（`hitl_login_detect`）**只在显式 `--tag hitl-live` 时**才会被选中（需可达登录墙站点，见 §4.3 / §6.5） |

### 6.4 真实模式 / 3 轮 acceptance（P4-D7）

```bash
cd browsepilot
BP_DEFAULT_MODEL=deepseek/deepseek-chat BP_MODEL_API_KEY=... \
  BP_BROWSER_CHANNEL=msedge \
  uv run python -m eval.run_eval --tag acceptance --repeat 3 --md data/eval-reports/
```

- `--repeat 3`：同一场景集完整跑 3 轮（量的是波动，不是抽样）；报告含每轮结果 + 跨轮方差（成功率/延迟 min-max + stdev）。
- `--md data/eval-reports/`（可再加 `--json data/eval-reports/`）：路径是目录 → 写 `<时间戳>.md`；目录会自动创建。`data/` 在 gitignore，报告不进 git —— 摘要与结论入 `docs/`。
- exit code：任一**非 fragile** 失败即 1；fragile 场景失败只计数（P4-D6：ddgs 内容漂移不是回归）。
- 单场景排查：`--tag smoke` 只跑确定性核心；先在 `--mock` 下复现（无需任何前提），再上真实模式。
- HITL 单场景（需真实可达的登录墙站点 + `BP_BROWSER_HEADLESS=false`）：`uv run python -m eval.run_eval --tag hitl-live` —— 它**不**在 `--tag acceptance` / `--tag smoke` / 无 tag 的默认全量跑里（§4.3）。

### 6.5 CI 说明（P5-T0b）：workflow 已就绪，但**从未在 GitHub Actions 上实际执行过**

- **事实**：`.github/workflows/ci.yml` 已写好（触发：push `master`/`v2.0` + PR；步骤 = Install →
  Lint → Types → Tests → Eval (mock)，全部 `working-directory: browsepilot`），但本地分支**不推到远程**
  （用户明确不推送）→ **没有任何一次 Actions 运行记录**。CI 口径目前只有"声明"，没有"执行证据"。
- **本地等价验证**：`bash browsepilot/eval/gate.sh`（§6.1）逐条跑 CI 的四步，无网络 / 无 key / 无浏览器；
  P5-T0b 当次（2026-09-11）全绿 **exit 0**：`pytest tests/ -q` = **868 passed**、`ruff check src/ eval/ tests/`
  = clean、`mypy src/browsepilot/` = clean、`python -m eval.run_eval --mock` = `passed=8 skipped=4 disabled=4`（exit 0）。
  **P5 后续读数（2026-09-11）**：本节的 868 是 P5-T0b 当次的历史读数；P5-T1 删除 v1 测试后为 **801**（达成于 `07bc3bd`，在 `3d51612` 时点实测）、
  P5-T3 新增 9 条 bench 测试后当前基线 = **810 passed**（§2 的 P5 刷新注 / §6.1）；gate 的命令与口径未变。
  语义与 CI **完全同口径**（同一个脚本、同一组命令、同一份 mock 场景集），差别只在"谁来跑"：CI 是
  ubuntu-latest + `uv sync --all-extras --frozen`，本地是当前开发机（Python 3.14，依赖由 `uv.lock` 钉住）。
- **发布报告必须如实标注**：引用 CI 时写"workflow 已就绪 + 本地 `gate.sh` 等价验证（exit 0）"，
  **不得**写"CI 通过 / CI 绿"——那是一条没有执行证据的声明（诚实原则：未验证 ≠ 通过，与 §8.2 同一口径）。
- **补口**：首次 `git push` 后的第一件事是核对一次真实的 Actions 运行结果，把结论（run id / 时间 / 结论）
  补进本节；若 Actions 上出现与本机不一致的失败，以 Actions 为准修口径。

## 7. 量化结果（T7/T8 完成）

> 数据来源（`data/` 在 gitignore，本节数字**逐字取自**下列产物，未采集的项写"未采集"）：
> `data/eval-reports/p4-t7.json|md`（acceptance 3 轮，2026-09-11T19:54:28+08:00）·
> `data/eval-reports/p4-t7-full.json|md`（全量 1 轮）· `data/eval-reports/p4-t7-hitl.json|md`（HITL headful 单场景）·
> 原始日志 `data/eval-reports/*-run.log`（含 `EXIT=` 行）· 复现探针原文 `data/eval-reports/t7-probe-t2searchcontent.txt`、`t7-probe-hitl.txt`。

### 7.1 运行前提记录（本次 3 轮 acceptance）

| 项 | 值 | 证据 |
|----|----|------|
| 时间窗 / 耗时 | 19:49:50（pool start）→ 19:54:28（报告落盘）＝ **4 分 38 秒** | run log；`environment.date` |
| 模式 / 模型 | real / `deepseek/deepseek-chat` | `environment.mode` / `environment.model` |
| 代码版本 | `61a6a28`（分支 v2.0） | `environment.git_commit` |
| 浏览器池 | `started`（headless=True, channel=msedge, max_pages=3） | `environment.browser_pool`；run log `BrowserPool started` |
| 凭据 | shell 内 `BP_MODEL_API_KEY` / `BP_MODEL_BASE_URL` ← `.env` 的 `OPENAI_API_KEY` / `OPENAI_BASE_URL`（仅进程内，未落盘、未入库） | §6.3；执行记录 |
| 必钉项 | `BP_DEFAULT_MODEL=deepseek/deepseek-chat`（思考模型拒绝 forced tool_choice → structured 全场景报错） | §6.3 |
| 运行形状 | `--tag acceptance --repeat 3`：10 条可执行场景 × 3 轮 = **30 次场景运行**；池跨 3 轮只起一次（不把起停噪声混进方差） | `environment.repeats=3` / `tags` |
| 每轮跳过 / 停用 | skipped=2（`degrade_browser_unavailable` mode=mock；`hitl_login_detect` 无 acceptance tag）＋ disabled=4（3 条 `p0_*` 归档 ＋ `t3_browser_interaction`） | 每轮 `stats` |
| exit code | **0**（无非 fragile 失败；1 条 fragile 失败只计数，不拦门禁） | run log `EXIT=0` |

### 7.2 3 轮结果（每轮都是完整一遍，量波动而非抽样）

| 轮次 | 执行 | 通过 | 失败（门禁） | fragile 失败 | 成功率 | P50 (ms) | P95 (ms) | 总 token |
|------|------|------|--------------|--------------|--------|----------|----------|----------|
| 1 | 10 | 9 | 0 | 1 | 90.0% (9/10) | 3893.5 | 20053.2 | 3427 |
| 2 | 10 | 9 | 0 | 1 | 90.0% (9/10) | 3173.4 | 28910.6 | 3398 |
| 3 | 10 | 9 | 0 | 1 | 90.0% (9/10) | 3221.3 | 33314.8 | 3366 |
| **样本池（30 次运行）** | **30** | **27** | **0** | **3** | **90.0% (27/30)** | **3221.3** | **33380.9** | **10191** |

样本池延迟 min / max = **1772.3 / 45503.1 ms**；30 次运行的 `stop_reason` 全部为 `completed`（无 partial / error），日志里 **0 条** `EXCEPTION` 行（仅有 `networkidle wait bounded out after 500ms` 的 DEBUG 记录——那是设计内的有界等待）。

3 轮中唯一的失败场景是 `t2_search_content`（`fragile=true`）：**3/3 轮 FAIL**，明细均为 `keywords hit=[]`。

**逐场景 × 逐轮明细**（单元格 = 结果 · 耗时 ms；token 为每轮该场景值，`—` = 该轮未采集到 token 快照）：

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

**分层（样本池口径）**：

| tier | 执行 | 通过 | 失败 | fragile 失败 | 成功率 | P50 (ms) | P95 (ms) | tokens |
|------|------|------|------|--------------|--------|----------|----------|--------|
| tier1 | 18 | 15 | 0 | 3 | 83.3% (15/18) | 11609.65 | 43327.11 | 6914 |
| tier2 | 6 | 6 | 0 | 0 | 100.0% (6/6) | 3213.58 | 4340.56 | 1720 |
| tier3 | 6 | 6 | 0 | 0 | 100.0% (6/6) | 2378.60 | 3090.64 | 1557 |

### 7.3 跨轮方差（总体标准差，P4-D7 要的"波动证据"）

| 指标 | 最小 | 最大 | 均值 | 标准差（总体） | 读数 |
|------|------|------|------|----------------|------|
| 成功率 | 0.9000 | 0.9000 | 0.9000 | **0.0000** | 3 轮结论完全一致 |
| 延迟 P50 (ms) | 3173.38 | 3893.53 | 3429.40 | **328.77** | ≈ 均值的 9.6%，相对稳定 |
| 延迟 P95 (ms) | 20053.17 | 33314.83 | 27426.20 | **5514.86** | ≈ 均值的 20.1%，被依赖延迟拉伸 |
| 总 token | 3366 | 3427 | 3397 | **24.91** | ≈ 均值的 0.7% |

读数：**成功率零方差**（每轮都是同一组 9 PASS + 1 fragile FAIL）。波动集中在延迟：P95 的 20.0s → 33.3s 摆动由 `t2_search_structural`（18.0s → 42.9s → 45.5s）与 `t1_weather_not_found`（12.1s → 11.8s → 17.4s）贡献，两者都走网络（ddgs / 天气服务），所以 P95 读的是**依赖延迟**；P50（3.2s–3.9s）才是相对稳定的产品侧读数。token 波动 <1% 说明模型侧调用量在 3 轮间基本恒定。

### 7.4 flaky 场景（跨轮结论不一致）

**0 个**（报告 `flaky: []`）。10 条执行场景在 3 轮里状态完全一致；唯一的失败 `t2_search_content` 是
**3 轮全 FAIL（一致失败）**，不是 flaky —— 3 轮同因同果正是 P1 "天气 100% 可靠"那类单轮结论所缺的方差证据：
稳定的通过集合可以复现，稳定的失败集合也可以归因。

### 7.5 FAIL 归因清单（产品缺陷 vs 评测/环境问题，分开列）

**A. 评测 / 环境问题（2 条，均非产品缺陷）**

| # | 场景 | 表现（逐字） | 复现证据 | 归因 |
|---|------|--------------|----------|------|
| A1 | `t2_search_content`（fragile，tier1） | 3/3 FAIL，明细 `keywords hit=[]`；运行本身 `stop_reason=completed`、`tools=['search']`、无降级日志 | 单场景复跑探针（19:55）答案原文："抱歉，根据现有数据无法找到 Python 3.13 的正式发布时间。搜索结果中没有相关信息。"（`tokens=432`，`chat_calls=1`/`model_calls=2`）；探针拿到的 ddgs 结果全部与问题无关（jieba 分词教程 / Python 3.10 发布说明 / OSCHINA 标签页 / Mac 安装文档 / Debian 博客） | **依赖内容漂移 + 产品诚实拒编（设计行为）**：断言 `contains_keyword: ["2024"]` 依赖 ddgs 返回内容，本环境返回里没有 Python 3.13 发布信息 → 断言不可能满足。这是 P4-D6 的 fragile 情形（P1 通过 → P2 漂移 → P4 结构化解耦的第三次演进），不是回归 |
| A2 | `hitl_login_detect`（非 fragile，tier3） | 全量 1 轮（headless）与 `--tag hitl` 专跑（headful）**都是 FAIL**，明细同为 `pending_human_action=False`，耗时 22729.3 / 29033.35 ms | 复现探针（headful）答案原文："抱歉，这个任务暂时无法完成：browser_open: 打开页面失败：https://github.com/login（TimeoutError: Page.goto: Timeout 30000ms exceeded … waiting until “domcontentloaded”）；browser_snapshot: 还没有打开的页面…"；`degradation_log` = `browser_open:timeout` + `browser_snapshot:no_open_page` → `all_strategies_failed` | **环境问题（场景站点不可达）**：本机到 github.com/login 的 `goto` 30s 超时 → 页面从未加载 → 登录墙检测（需采样可见文本/密码框）不可能触发。场景注释假设"github.com 国内可达"在本机实测下**不成立**。产品的失败路径是对的（诚实失败模板、不编造、工具调用有记录）。headful 与 headless 表现一致 → 排除"headless 才测不出"的假设。**HITL 的 `needs_human` 链路结论 = 未获验证**（既不是通过，也不是已确认缺陷） |

**B. 产品缺陷（acceptance 3 轮内 0 条）**

- 3 轮 acceptance 的**非 fragile 失败 = 0**，10 条执行场景（tier1/2/3 + 多轮 + 降级路径）未观察到产品缺陷；例外路径也有正向证据：`t2_read_page_failure` 3/3 PASS（DNS 失败 → 明确报错、无 Traceback、不编造），`t3_browser_open/snapshot` 3/3 PASS，`multi_turn_context` 3/3 PASS（第二轮问"那明天呢？"，最终答案含"北京" → 历史注入 answer 生效）。
- 记录两条**未验证 / 已声明缺口**（不是本轮 FAIL）：`t3_browser_interaction` 处于 `enabled: false`（交互链未入图，P3 遗留）；`multi_turn_context` 的 YAML 已声明缺口——`result_data` 带 `error`（策略链全败）时 answer 模板不注入 history——本轮 3 轮都走了正常路径，**该缺口未被触发**，仍是未验证风险。

### 7.6 补充运行（全量一轮 / HITL / 冒烟）

| 运行 | 命令形状 | 结果 | 耗时 | exit |
|------|----------|------|------|------|
| 冒烟（先行验证凭据/网络） | `--tag smoke --repeat 1`（real, headless） | passed=6 failed=0 fragile_failed=0 skipped=6 disabled=4 | 19:47:58 → 19:49:19（**81s**） | 0 |
| 全量一轮 | 无 tag，`--repeat 1`（real, headless） | executed=11（10 acceptance + hitl），passed=9 failed=**1** fragile_failed=1 skipped=1 disabled=4，成功率 81.8% (9/11)，P50 4828.85 / P95 20007.47，tokens 3378（9 条样本） | 19:55:41 → 19:57:17（**1m36s**） | **1**（唯一非 fragile 失败 = `hitl_login_detect`） |
| HITL headful 专跑 | `--tag hitl --repeat 1`（`BP_BROWSER_HEADLESS=false`） | executed=1，passed=0 failed=1，耗时 29033.35 ms，tokens **未采集**（该场景 0 次模型调用） | 19:59:16 → 19:59:46（**30s**） | **1** |

冒烟先行的理由：把"凭据/网络/浏览器池是否可用"和"3 轮验收"分开——冒烟 6/6 PASS 之后才开 3 轮，否则 45 分钟里的失败会混进环境噪声。四份原始日志都在 `data/eval-reports/`（含 3 轮的 `p4-t7-run.log`）。

> **P5-T0b 口径变更（2026-09-11）**：上表"全量一轮"的 `executed=11` 是**当时**的默认跑口径（无 tag 选全部 mode 匹配的场景，含 `hitl_login_detect`）。此后该场景标 `hitl-live` → 无 tag 的默认全量跑**不再选它**（改为打印跳过原因），同一命令现在是 `executed=10`，exit code 不再由这条**环境不可达**的失败决定（口径见 §4.3 / §6.4）。历史数字保持原样（它是当时的真实读数），引用时按本注读。

### 7.7 数据可信度限制（读这节数字前先读这里）

- **token 是 chat-only 下限**：`usage_source=chat_only`——`structured`/`stream` 调用没有 usage 面，不进总和。3 轮样本池里 **30/63 次模型调用有计量（47.6%）**，所以 10191 是**下限**而不是"总消耗"（报告里就是这个口径，每场景还带 `measured_calls/model_calls`）。
- **成本未采集**：3 轮 `cost_samples=0`、`total_cost_usd=null`。每次运行启动时 LiteLLM 取远端价格表失败（log：`Failed to fetch remote model cost map from https://raw.githubusercontent.com/… : timed out. Falling back to local backup`），本地备份也没给出该模型的成本 → 写"未采集"，不估算。
- **1 条场景天然没有 token 数**：`t2_read_page_failure` 3 轮 token 均缺席——它走模板答案路径，是"未知不是 0"的如实处理（所以每轮 `token_samples=9/10`），不是采集故障。**P5-T0 口径注**：当时 `token_usage` 整条为 `null`；同一形状在 P5-T0 之后会发覆盖度快照（`chat_calls=0`、`model_calls=2` —— fetch_page / browser_open 各一次 structured 参数提取，无 token 键），历史数字按本注读。
- **延迟读数含依赖网络时间**：P95 跨轮 20053 → 33315 ms（stdev 5514.86），单条极值 45503.05 ms（R3 `t2_search_structural`）；这把 P95 变成"依赖延迟 + 模型时间"的混合读数，引用时别只贴 P95。
- **样本量**：3 轮 × 10 场景 = 30 次场景运行（每场景 3 次）。这是**同一场景集重复跑**的波动，不是跨模型 / 跨日 / 跨网络的抽样；换模型（或换网络出口）结论不保证迁移。
- **mock 数据不代表模型质量**：CI 门禁跑 FakeProvider/FakeExecutor（§6.2），只能证明断言与管线可复现；本节所有数字都是真实模式，二者不可混用。
- **p0-search 归档与本节的关系**：`p0-search` 已归档（`enabled: false`），其内容断言的当前形态就是 `t2_search_content`（同 prompt、同 keywords、同样 fragile）；跨阶段对照（P1 通过 → P2 漂移 → P4 结构化）见 P4 报告 / 文章 4 素材。

## 8. 已知限制（T8 完成）

> 每条写"缺失面 → 当前读数 → 影响"；数字与证据指向 §7 的产物，未采集/未验证的如实标注（不估算）。

### 8.1 token 是 chat-only 下限；cost 本次未采集

- **token 口径**：`structured` / `stream` 没有 usage 面（§5.3）→ 只有 `chat` 调用计入，`model_calls` 是诚实分母。T7 三轮样本池 **30/63 次模型调用有计量（47.6%）**，`total_tokens=10191` 是**下限**而不是"总消耗"；每轮的 `token_samples`（3 轮均为 9/10）暴露覆盖面。引用这些数字时必须带上"下限"口径。
- **cost 未采集**：3 轮 `cost_samples=0`、`total_cost_usd=null`。原因：每次运行启动时 LiteLLM 取远端价格表失败（`Failed to fetch remote model cost map from https://raw.githubusercontent.com/… : timed out. Falling back to local backup`），本地备份也没有该模型的价格 → 写"未采集"，不估算。
- **"缺席"不是 0**：`t2_read_page_failure` 3 轮 token 均缺席（模板答案路径，0 次 chat 调用）；报告按"未知"处理（`null` / 样本数），不是采集故障。P5-T0 起"没有 token 数字"与"整条 `token_usage` 缺席"是两回事——覆盖度键照发（§5.2/§5.3）。

### 8.2 HITL 链路未端到端验证（环境阻塞）

- HITL 不在 `--tag acceptance` 的 3 轮里；补充运行（全量 1 轮 headless + `--tag hitl` headful 专跑）**都是 FAIL**，明细同为 `pending_human_action=False`。
- 直接原因：本机到 `github.com/login` 的 `Page.goto` **30s 超时**（headless/headful 同因）→ 页面从未加载 → 登录墙检测（需采样可见文本/密码框）不可能触发；场景注释假设的"国内可达"在本机不成立（§7.5 A2 的探针原文）。
- 结论：`needs_human` 链路 = **未验证**（既不是通过，也不是已确认缺陷）。要闭环需要**可达的登录墙站点**（P5 建议：换站点或本地 fixture 登录页）。
- 读数影响：全量 1 轮的 exit=1 就是由这一条非 fragile 失败构成的（§7.6）；它的计数在"产品失败"里，但归因是**环境**，报告环境行/探针原文已把这条链路分开列。
- **P5-T0b 处置（裁决 b，2026-09-11）**：新增 tag `hitl-live` 并把它从**默认全量跑**里摘出去（`run_eval.EXPLICIT_ONLY_TAGS`：无 `--tag` 不选它、跳过时打印原因；要跑就 `--tag hitl-live`）。无 tag 的 real 全量跑从此不再被这条环境失败污染 exit code，而 `--tag acceptance` 本来就不含它（§4.3 / §6.4）。**没有**改断言、**没有**标 `fragile`（fragile 的口径是"外部内容漂移"，这里是"环境不可达"，两回事）、也**没有**删场景（链路仍是"未验证"，不是"已通过"）。
- **仍缺的证据**：`needs_human` 链路的端到端验证 —— 需要一个可达的登录墙站点（或本地 fixture 登录页；注意 `NavigationPolicy` 白名单，`file://` 会被拦，fixture 得挂在允许的 host 上）。做到后可把它降回 `hitl`/`acceptance` 口径（P5+ 可选改进）。

### 8.3 fragile 只解决"不误报回归"，不解决"可复现"

- `t2_search_content`（fragile）依赖 ddgs 返回里含 "2024"；本环境返回无关内容 + 模型诚实拒编是设计行为（§7.5 A1）→ **3/3 FAIL 是环境读数，不是回归**，不计 exit code。
- 它的通过依赖外部数据的可得性：换网络出口 / 换时点结论可能不同。结构面由 `t2_search_structural` 守（工具链 + 无降级 + completed）；**内容面的"可复现"无法用断言保证**，只能靠 fragile 如实记录。同理，任何新写的"依赖外部内容"断言都必须走 fragile 口径，不得混进结构场景（§3.1）。

### 8.4 tier3 交互链未接入（`enabled: false`）

- `browser_type` / `browser_click` 已在 executor 实现（含 `BrowserParams` 参数模型），但 `graph._STRATEGY_CHAIN["browser_task"] = [browser_open, browser_snapshot]` **没有把交互步编排进链**。现在启用 `t3_browser_interaction` 只会稳定得到 `all_strategies_failed`。
- 该场景保留 `enabled: false`（P4 明确不做交互链扩展），断言写的是"启用后的预期语义"；`degradation_absent` 已补上（否则失败文案回显"输入 Python"会让它假通过，§3.2）。
- 连带缺口：type/click 后的页面采样与重定向复查（executor 的四处复查点）**无场景覆盖**。

### 8.5 多轮的架构缺口（已声明、未触发）

- **全败路径不注入 history**：`answer` 节点三条路径中，`result_data` 带 `error` 时走"抱歉，这个任务暂时无法完成"模板，**不注入 history**（chitchat/knowledge_qa 直答路径与数据路径都注入）。3 轮 acceptance 里 `multi_turn_context` 都走正常路径，**该缺口未被触发** → 仍是未验证风险（场景 YAML 已如实记录，触发时会 FAIL 而不是被放宽）。
- **classify / 参数提取只看任务原文**（不看 history）：第二轮流到哪个工具、能否提取城市不确定 → 场景**不**断言 `tools_called`（避免写一条必然抖动的断言）；这是有意的口径，不是遗漏。

### 8.6 运行环境前提与 `BP_` 前缀边界

- **浏览器 channel/rev**：本机 Playwright 1.59.0 需要的 chromium rev 不存在于本机缓存（§1）→ 真实 tier3 运行需 `BP_BROWSER_CHANNEL=msedge`（或 `uv run playwright install chromium`）。池起不来时 harness 打 WARNING + `attach_browser(None)`，报告环境行写 `unavailable (start failed)` —— tier3 的失败**在报告里**归因环境而不是任务（§6.3）。
- **`Settings` 只读 `BP_` 前缀**（`env_prefix="BP_"`，`extra="ignore"`）：v1 的 `OPENAI_API_KEY` / `OPENAI_BASE_URL` 不会被自动读取；必须显式映射成 `BP_MODEL_API_KEY` / `BP_MODEL_BASE_URL`（T7 的做法是 shell 内导出，值来自 `.env`，未落盘，§7.1）。
- **模型必须钉非思考模型**：structured 走 forced tool_choice（`src/browsepilot/model/litellm_provider.py` 的 `tool_choice={"type": "function", …}`），思考模型（deepseek-flash / v4-pro）拒绝它 → `StructuredOutputError`（structured 全场景报错）。所以真实运行必须 `BP_DEFAULT_MODEL=deepseek/deepseek-chat`（§6.3/§7.1；也是 `src/browsepilot/app/config.py` 的缺省值）。
- HITL 还需 `BP_BROWSER_HEADLESS=false`（可见窗口），且如上所述仍被站点可达性阻塞。

### 8.7 小样本统计的读数边界

- P50/P95 是**线性插值**（numpy 默认口径）：空集 → `None`，n=1 → 该样本本身；样本数由 `latency_samples` 暴露（T7：样本池 30；per-tier 18/6/6）。样本少时别把 P95 当结论。
- 延迟读数含**依赖网络时间**：P95 跨轮 20053.17 → 33314.83 ms（stdev 5514.86），由 `t2_search_structural`（18.0s → 42.9s → 45.5s）与 `t1_weather_not_found`（12.1s → 11.8s → 17.4s）贡献 —— 引用时别只贴 P95，P50 才是相对稳定的产品侧读数（§7.3）。
- 3 轮是**同一场景集重复跑**（量波动，不是抽样）：不是跨模型 / 跨日 / 跨网络的抽样，换模型或换网络出口结论不保证迁移（§7.7）。
- **mock 数据不代表模型质量**：CI 门禁跑 FakeProvider/FakeExecutor，只能证明断言与管线可复现；§7 的数字全部来自真实模式，二者不可混用（报告里有 mock banner）。

### 8.8 未验证 / 未采集清单（P5 输入）

| 项 | 状态 | 证据 |
|----|------|------|
| HITL `needs_human` 链路 | **未验证**（站点不可达；P5-T0b 已移入 `hitl-live`，默认跑不再选它） | §7.5 A2；§8.2 |
| `cost_usd` / 成本统计 | **未采集**（价格表超时） | §7.7；§8.1 |
| 多轮"全败路径不注入 history" | **未验证**（未触发） | §7.5 B；§8.5 |
| tier3 交互链（type/click） | **未接入**（`enabled: false`） | §8.4；`graph.py` 的 `_STRATEGY_CHAIN` |
| 跨模型 / 跨网络稳定性 | **未采集**（3 轮同环境） | §7.3；§8.7 |
| `tokens_below` / `latency_below` 进场景集 | **未使用**（引擎与单测就绪，无场景声明） | §5.1；16 文件的 check 类型统计 |

