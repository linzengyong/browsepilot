# V2.0 P4 交付报告（P4 Task 9 验收）

- 分支：`v2.0`（P4 提交范围 `ac3286d..HEAD`，14 个提交 = 13 个 P4 功能/文档 + 1 个 P3 收口 follow-up `db44335`；本报告为第 15 个）
- 验收日期：2026-09-11（本地 20:10 起，UTC 12:10 起；T7 真实 3 轮数据采集于 19:49–19:54）
- 范围：P4 评测体系 —— 检查引擎 v0.3（10 种断言）、场景集 v2（16 文件：tier1/2/3 + HITL + 多轮 + 降级）、真实模式 harness（BrowserPool 注入 + 逐场景 `aclose()` 隔离 + 用量/耗时/工具名采集）、双格式量化报告（成功率/延迟/token/per-tier/跨轮方差）、p0-search 结构化解耦 + fragile 标记、CI mock 回归门禁、真实模式 3 轮数据与失败归因、评测体系文档
- 验收结论：✅ **全量 852 passed + ruff/mypy 全绿；eval mock `passed=8/skipped=4/disabled=4`（exit 0）；真实模式 acceptance 3 轮成功率 90.0%（27/30）×3 完全一致、flaky 0 条、非 fragile 失败 0 条；`backend/`、`browser_mcp/` 零改动（冻结延续）；本任务（T9）仅新增 2 个 docs + 1 行文章索引，无 `src/`、`eval/`、测试改动**

---

## 一、全量测试与工程化门禁（实测）

工作目录 `D:/AI_Agent_Demo/browsepilot`，`PYTHONIOENCODING=utf-8`。

```text
$ uv run pytest tests/ -q
852 passed, 1 warning in 16.15s

$ uv run ruff check src/ eval/ tests/
All checks passed!

$ uv run mypy src/browsepilot/
Success: no issues found in 32 source files

$ uv run python -m eval.run_eval --mock
[eval] passed=8 failed=0 fragile_failed=0 skipped=4 disabled=4 total=16   # exit 0
```

- 增长轨迹：P3 收口 `ac3286d` **637** → P4 收口 **852**（+215）：637→664(T1)→701(T2)→717(P3 follow-up)→744(T2-fix)→764(T3)→806(T4)→823(T3-fix)→841(T4-fix)→846(T5)→852(T5-follow-up)；T6/T7/T8 为 CI/数据/文档，测试数不变。
- 唯一 warning：`backend/app/config.py:19` PydanticDeprecatedSince20（冻结旧代码，与 P1/P2/P3 相同，不修）。
- mock 计数口径：16 文件 = 8 可 mock 执行 + 4 mock 下 SKIP（real-only）+ 4 DISABLED（3 条 p0-* 归档 + `t3_browser_interaction` 待启用）；判据是 exit code（0）。
- `tests/test_eval/` 共 204 个测试（`test_checks.py` 28 / `test_harness.py` 27 / `test_report.py` 60 / `test_scenarios.py` 89），是 P4 新增的主体（P4 阶段新增/改写测试集中在 `tests/test_eval/` 与 `tests/test_agent/test_graph.py`）。

## 二、评测体系交付清单（T0–T9）

| # | 交付项 | commit | 内容与证据 |
|---|--------|--------|------------|
| T0 | 基线锁定 + 环境节 | `3040473` | `docs/v2-eval.md` 骨架（§1 环境 / §2 启动基线 637 passed）；P4 起点快照 |
| T1 | 检查引擎 v0.3（P4-D1 全 10 类型） | `ef3ee26` | `eval/checks.py`（纯函数、无 I/O）；`eval/run_eval.py::check_outcome` 保留 tuple 形状的兼容薄层；`tests/test_eval/test_checks.py`（28）——空声明/未知 mode 一律 FAIL，不抛异常 |
| T2 | 场景 schema v2 + 场景集 | `be3e9a0` | `eval/scenarios.py`（`extra="forbid"`、tags/mode/timeout/fragile/enabled/follow_up）；16 个 YAML；`test_scenario_set_matches_the_p4_inventory` |
| T2-fix | 断言加固（假通过红线） | `dddb99b` | 见 §五 D3；RED 10 用例 → 23 个合成回归网进入 CI |
| T3 | 真实模式 harness（P4-D5）+ 采集 | `b86d5ed` | `eval/harness.py`（`RealDeps`/`ToolCallRecorder`/`UsageAccumulator`/`UsageTrackingProvider`/`pool_lifecycle`）；BrowserPool 注入 + 逐场景 `aclose()` + 多轮接线 |
| T3-fix | 隔离与自描述修复 | `21d454b` | C1：单场景异常（含超时）不再中止整轮；I2：usage 自描述（`chat_calls`/`usage_source`/`model_calls`） |
| T4 | 统计聚合 + 双格式报告（P4-D3/D4） | `bf4bc44` | `eval/report.py`（`ScenarioResult`/`aggregate`/`write_json`/`write_markdown`）、`--json/--md/--repeat N` |
| T4-fix | 报告诚实性修复 | `a6c8b08` | I1：未测指标不渲染为 0（逐轮样本数门控）；I2：环境归因入报告（pool 状态 + tier3 警告行） |
| T5 | p0-* 归档 + 结构化解耦 | `61e1b98` | 3 条 p0-* 转 `enabled: false`；断言逐条移交（`_P0_SUCCEEDED_BY` + `test_p0_assertion_semantics_have_enabled_successors`）；移除结构版的措辞断言 |
| T5-fu | 产品修复（评测→修复闭环） | `25b1c76` | `_degraded_provenance` + `_DEGRADED_ANSWER_RULE`：降级成功路径必须声明来源；见 §五 D7 |
| T6 | 回归门禁（P4-D8） | `61a6a28` | `eval/gate.sh`（pytest→ruff→mypy→eval mock，`set -euo pipefail`）+ `.github/workflows/ci.yml` 加 `Eval (mock)` 步骤；本地 `bash eval/gate.sh` 实测 exit 0 |
| T7 | 真实 3 轮 + 方差 | `06382b1` | `docs/v2-eval.md` §7（+105 行）；产物 `data/eval-reports/p4-t7.json|md` + `p4-t7-run.log`；35/35 项与 §7 自动交叉检查一致 |
| T8 | 评测体系文档 | `d93ea09` | `docs/v2-eval.md` 211→501 行（§3 哲学 76 + §4 场景规范 121 + §5 类型表 47 + §8 限制 58）；§4 示例与 YAML 逐字一致、清单与 `load_scenarios()` 逐行一致 |
| T9 | 文章 4 素材 + 本报告 | 本提交 | `docs/v2-articles/article-4-material.md` + `docs/v2-p4-report.md` + `docs/v2-articles/README.md` 文章 4 状态行 |
| — | P3 收口 follow-up（P4 期间派发） | `db44335` | 错误页归因/click 提升收窄/hitl resume 语义（P3 §十二 的 3 项 New Issues；717 passed） |

## 三、量化结果与失败归因（T7，数据来源 `data/eval-reports/p4-t7.json`）

**3 轮 acceptance（`--tag acceptance --repeat 3`，real / `deepseek/deepseek-chat` / msedge headless，30 次场景运行，19:49:50→19:54:28）**

| 轮次 | 执行 | 通过 | 失败（门禁） | fragile 失败 | 成功率 | P50 (ms) | P95 (ms) | 总 token |
|------|------|------|--------------|--------------|--------|----------|----------|----------|
| 1 | 10 | 9 | 0 | 1 | 90.0% | 3893.5 | 20053.2 | 3427 |
| 2 | 10 | 9 | 0 | 1 | 90.0% | 3173.4 | 28910.6 | 3398 |
| 3 | 10 | 9 | 0 | 1 | 90.0% | 3221.3 | 33314.8 | 3366 |
| 样本池 | 30 | 27 | 0 | 3 | 90.0% (27/30) | 3221.3 | 33380.9 | 10191 |

- **方差**：成功率 stdev **0.0000**（3 轮完全一致）；P50 stdev 328.77 ms（≈9.6%）；P95 stdev 5514.86 ms（依赖延迟拉伸）；token stdev 24.91（≈0.7%）。
- **flaky 场景 0 条**（`flaky: []`）；唯一失败 `t2_search_content` 3/3 轮一致 FAIL（fragile，`keywords hit=[]`）→ 不是 flaky，是已知内容漂移。
- **exit code 0**（无非 fragile 失败）；30 次运行 `stop_reason` 全部 `completed`，日志 0 条 `EXCEPTION`。
- **FAIL 归因 2 条，均非产品缺陷**：A1 `t2_search_content`（ddgs 内容漂移 + 模型诚实拒编 = 设计行为，探针原文在 `t7-probe-t2searchcontent.txt`）；A2 `hitl_login_detect`（本机到 `github.com/login` 的 `goto` 30s 超时 → 登录墙检测不可能触发；headful/headless 同因 → **HITL `needs_human` 链路 = 未验证**）。
- **产品缺陷 0 条**；失败路径有正向证据：`t2_read_page_failure` 3/3 PASS、`t3_browser_open/snapshot` 3/3 PASS、`multi_turn_context` 3/3 PASS。
- 补充运行（T7 取证，不属于 3 轮）：冒烟 6/6（81s，exit 0）；全量 1 轮 executed=11/passed=9/failed=1（HITL 环境失败）/exit 1；HITL headful 专跑 0/1（30s）。
- **未采集/如实标注**：`cost_usd` 全部为 `null`（LiteLLM 价格表超时，`cost_samples=0`）；token 是 chat-only 下限（30/63 次调用有计量，47.6%）；`t2_read_page_failure` 3 轮 token 缺席（模板路径 0 次模型调用，"未知不是 0"）；P95 含依赖网络时间。

## 四、验收对照（plan §P4 交付检查单 + spec §5 P4 行 + §7.2 红线）

### 4.1 plan §P4 交付检查单

| 交付项 | 结果 | 证据 |
|--------|------|------|
| 检查引擎 v0.3（全类型，向后兼容） | ✅ PASS | `ef3ee26`；`eval/checks.py` + `run_eval.check_outcome` 兼容层；`tests/test_eval/test_checks.py`（28） |
| 场景集 ≥12 个（tier1/2/3 + hitl + multi-turn + degrade；fragile 生效） | ✅ PASS | 16 个 YAML（`be3e9a0` + `61e1b98`）；`test_scenario_set_matches_the_p4_inventory`；mock `passed=8 skipped=4 disabled=4` |
| 真实模式 harness（browser 注入 + 逐场景隔离） | ✅ PASS | `b86d5ed` + `21d454b`；`tests/test_eval/test_harness.py`（27：fake pool aclose/注入/失败不中断）；§三 3 轮只用一次池 |
| 量化报告（JSON + Markdown；成功率/延迟/token/成本） | ✅ PASS（成本项未采集） | `bf4bc44` + `a6c8b08`；`p4-t7.json`（环境/汇总/per-tier/方差/flaky）+ `p4-t7.md`；`cost_usd=null` 如实标注（`cost_samples=0`） |
| p0-search 结构化解耦（漂移不再导致门禁失败） | ✅ PASS | `61e1b98`；`t2_search_structural` 3/3 轮 PASS；`t2_search_content` 3/3 轮 FAIL 但 fragile 不计 exit code；`test_structural_search_scenario_does_not_assert_on_answer_wording` |
| CI 门禁（mock 全绿；本地 gate 脚本） | ✅ PASS（GH Actions 未实际执行） | `61a6a28`；`eval/gate.sh` 4 步；`ci.yml` 加 `Eval (mock)`；本地 `bash eval/gate.sh` exit 0（852 passed + mock passed=8；T6 记账）；GH 侧以 `yaml.safe_load` 验证，未跑真实 runner |
| 真实模式 3 轮数据 + 方差 | ✅ PASS | `06382b1`；§三/`docs/v2-eval.md` §7；flaky 0、成功率 stdev 0 |
| 评测体系文档 `docs/v2-eval.md` | ✅ PASS | `d93ea09`；501 行（§4 示例与 YAML 逐字一致、§4.6 清单与 `load_scenarios()` 逐行一致） |
| 文章 4 素材 + P4 报告 | ✅ PASS | 本提交；`article-4-material.md`（场景演进/schema/断言类型/3 轮数据/门禁/两个真实案例/漂移三次演进）+ 本报告 + README 文章 4 状态行 |
| 冻结检查：`backend/`、`browser_mcp/` 零改动 | ✅ PASS | 见 §七（命令输出为空） |

### 4.2 spec §5 P4 行

| spec 行 | 结果 | 证据 |
|---------|------|------|
| 阶段目标：评测集完善 + 回归门禁 · 量化报告 | ✅ | 16 场景集 + `gate.sh`/CI mock + `report.py` 双格式量化 |
| 交付物：评测体系文档 + 报告 | ✅ | `docs/v2-eval.md`（501 行）+ 本报告 + `data/eval-reports/p4-t7.json|md`（gitignored，摘要入 §7） |
| 文章 4（评测）素材 | ✅ | `docs/v2-articles/article-4-material.md` |

### 4.3 spec §7.2 质量红线

| # | 红线 | 结果 | 证据 |
|---|------|------|------|
| 1 | agent 执行效果第一位（P0 场景可靠优先） | ✅ | 3 轮 acceptance 非 fragile 失败 0；P0 语义全部有启用承接者且 3/3 PASS（`t1_weather_beijing`/`shanghai`、`t2_read_page`、`t2_search_structural`） |
| 2 | 代码可读、可测、职责单一 | ✅ | ruff clean + mypy clean（32 files）；`checks.py` 纯函数无 I/O；852 tests（`tests/test_eval/` 204） |
| 3 | 分层单向依赖（禁止跨层 import） | ✅ | `tests/test_architecture.py`（4 守卫）在套件内；`grep -rn "import eval\|from eval" src/` 零命中（评分层不反向依赖） |
| 4 | 每阶段结束：pytest 通过 + P0/P1 评测回归通过 | ✅ | 852 passed + `gate.sh` mock 全绿（exit 0）+ 真实 3 轮 90.0%/flaky 0 |
| 5 | 小步迭代、每增量汇报 | ✅ | 14 commits / T0–T8，每 Task ≥1 commit；fix 轮次独立提交（`dddb99b`/`21d454b`/`a6c8b08`/`25b1c76`） |

**plan「P4 明确不做」确认未做**：自研评测平台 UI（无）· LLM-as-judge（无实现，确定性断言）· 多模型横评（无）· tier3 交互链扩展（未做，场景保持 `enabled: false`）。

## 五、偏差与裁决摘要（D1–D8）

| # | 偏差 / 裁决 | 结果与理由 | 证据 |
|---|-------------|------------|------|
| D1 | **p0-* 归档**（plan 未写，T5 决定） | 3 条历史基线转 `enabled: false` 而非删除；断言逐条移交（`temperature_c` → `t1_weather_beijing`；结构 → `t2_search_structural`；内容 → `t2_search_content`），测试钉住"缺承接者 = 丢断言" | `61e1b98`；`docs/v2-eval.md` §3.5；`test_p0_assertion_semantics_have_enabled_successors` |
| D2 | **门禁文档落点**（plan §Task 6 写 `browsepilot/README.md` 已陈旧——README 已移到仓库根且带他人未提交改动） | 控制器裁决：门禁说明写入 `docs/v2-eval.md` §6 运行指南（正式交付物，零污染/零 stash 风险），不新建 `browsepilot/README.md`；根 README 的提及留 P5 文档节 | ledger T6 行；`docs/v2-eval.md` §6 |
| D3 | **T2-fix 的由来**（T1+T2 合并审查 1 Critical：断言虚假通过） | `dddb99b`：证据型关键词 + `degradation_absent` + `stop_reason_is`；RED 10 用例（5 条正例场景 × 全失败/partial）→ 23 个合成回归网进 CI；真图交叉验证（`data/traces/probe-*.jsonl`） | `dddb99b`；`tests/test_eval/test_scenarios.py`；本报告 §六 |
| D4 | **T3 范围扩容**（plan T3 之外） | T1 探测发现 outcome 无 token/耗时/工具名字段（`graph.answer` 丢弃 `ChatResult.usage`；`execution_log` 在 v2 永远空）→ T3 扩为"harness + 用量/耗时采集"（ledger 记"计划调整"） | ledger T3 前 "探测发现" 段；`b86d5ed`；`docs/v2-eval.md` §5.2/§5.3 |
| D5 | **T3-fix 的由来**（T3 审查 NEEDS FIXES） | `21d454b`：C1 场景异常（含超时）曾中止整轮（与 P4-D5 矛盾）→ 逐场景 try/except + `[FAIL] EXCEPTION` 继续；I2 usage 自描述（`chat_calls`/`usage_source`/`model_calls`）；实测 a-raise→b-pass `passed=2 failed=1 exit 1` | `21d454b`；ledger T3 fix 行 |
| D6 | **T4-fix 的由来**（T4 审查 NEEDS FIXES） | `a6c8b08`：I1"未测到 token"在方差段/JSON 渲染为 0（与汇总"—（未提供）"自相矛盾）→ 逐轮样本数门控；I2 浏览器池启动失败不进报告（tier3 全 0% 无法归因环境）→ 环境块 + MD 警告行 | `a6c8b08`；ledger T4 review 行 |
| D7 | **T5-follow-up 产品修复**（越出"只改评测"） | 评测抓出真实产品缺陷（`t1_weather_not_found` 真实 4/5 FAIL：search 兜底隐式换实体）→ `25b1c76` 改 `src/browsepilot/agent/graph.py`（`degraded_from` + 降级回答规则）；复验 `t5-fix.json` 6/6 PASS | `25b1c76`；`data/eval-reports/p4-t5-prefix.json` / `t5-fix.json`；`article-4-material.md` §5 |
| D8 | **真实运行环境事实**（影响运行口径） | 必须显式 `BP_DEFAULT_MODEL=deepseek/deepseek-chat`（思考模型拒绝 forced tool_choice → structured 全场景报错）+ `BP_BROWSER_CHANNEL=msedge`（本机 chromium rev 不匹配）；key 仅 shell 进程内（`.env` 的 v1 变量映射，未落盘）——plan 的裸命令样例不可直接使用 | `docs/v2-eval.md` §1/§6.3/§7.1/§8.6；ledger T5 行 |

## 六、遗留清单（carry-over to P5）

> 来源：`docs/v2-eval.md` §8.8、P3 §八（未关闭项）、P4 ledger 残留、T2-fix/T5 残留。均**未在 P4 修复**。

| # | 项 | 状态 / 影响 | 出处 |
|---|----|-------------|------|
| 1 | **tier3 交互链未接入**（`browser_type`/`browser_click` 未编排进 `_STRATEGY_CHAIN`） | `t3_browser_interaction` 只能 `enabled: false`；type/click 后的页面采样/重定向复查无场景覆盖 | `docs/v2-eval.md` §8.4；P3 §八 第 2 条（部分关闭） |
| 2 | **多会话共享 current page** | executor 是 app 级共享对象，多会话并行会互相串页（会话级隔离需把 page 放进会话上下文）；`executor.aclose()` 也未接 app lifespan（P4 只在 harness 逐场景调用） | `src/browsepilot/app/executor.py` 模块 docstring + `aclose()` docstring；P3 ledger Minor |
| 3 | **`tokens_below` / `latency_below` 无场景声明** | 引擎与 28 个单测就绪，但预算门禁未进场景集（token 是 chat-only 下限，直接当硬预算会误判） | `docs/v2-eval.md` §5.1 注 / §8.8 |
| 4 | **`cost_usd` / 成本统计未采集** | 价格表超时；3 轮 `cost_samples=0`、`total_cost_usd=null` | §7.7 / §8.1 |
| 5 | **HITL 链路未端到端验证** | 需可达的登录墙站点（本机 github 30s 超时）；`needs_human` 既不是通过也不是已确认缺陷；resume 后真实浏览器链路仍未测 | §7.5 A2 / §8.2；P3 §八 第 6 条 |
| 6 | **multi_turn 架构缺口（已声明、未触发）** | 全败路径 `answer` 不注入 history；`classify`/参数提取只看任务原文 → 场景不断言 `tools_called`（避免必然抖动） | §8.5；`multi_turn_context.yaml` 注释 |
| 7 | **router 强词问题式短语** | "为什么京东比淘宝便宜" → tier3（强词不受求知守卫约束）；规则调优待评测数据说话 | P3 §八 第 10 条；`article-3-material.md` 残留节 |
| 8 | **P3 §八 其余 Minor** | `waited` 信号恒 False；JS 回退成功语义（"已派发≠已生效"）；无 press/submit 键盘原语；`scroll(...,inf)` OverflowError；redirect-then-navigate-failure 不复查；detect 的 `role="dialog"` 规则已删；`BP_BROWSER_CHANNEL` 运维注记（已入 §8.6） | P3 §八 第 8–10 条 |
| 9 | **报告不存 answer 全文** | 失败/修复取证只能靠 ledger 记账与探针日志（案例 2 的答案原文即如此） | ledger T5 行 |
| 10 | **兜底成功后 `stop_reason` 仍是 `completed`** | spec 只要求"带说明"，未引入 degraded 终止原因（评测无法从 `stop_reason` 区分兜底成功） | ledger T5 残留 |
| 11 | **t3 证据关键词依赖模型引用页面标题** | `Example Domain` 若被真实模型全中文改写可能假 FAIL（保守方向；3 轮真实运行未触发） | ledger T2-fix 残留 |
| 12 | **`mypy eval/` 有 16 个既有错误**（`run_eval.py:242` 的 `**dict` 解包） | gate.sh / CI 只查 `src/browsepilot/`；eval/ 未纳入类型门禁 | 实测 `uv run mypy eval/`；ledger T3-fix 注 |
| 13 | **GH Actions 未实际执行** | CI YAML 以 `yaml.safe_load` 验证过语法；`gate.sh` 的索引是 LF、工作树可能被 `core.autocrlf` 转 CRLF（同 `start.sh`，CI 无影响） | ledger T6 残留 |
| 14 | **跨模型 / 跨网络稳定性未采集** | 3 轮 = 同一场景集重复跑（量波动，不是抽样） | §8.8 |

> 已关闭项（不再带入 P5）：P3 §八 第 1（click 跨页假失败）、第 3（`_quarantine_page` release）、第 5（answer 不读 `human_response`）、第 4（池 `close()` 已入 lifespan）、第 7（tier3 `all_strategies_failed` 已在 T7 HITL 探针获得真实样本：`browser_open:timeout` + `browser_snapshot:no_open_page`）。

## 七、范围与冻结确认（out-of-scope）

```text
$ git diff --stat ac3286d..HEAD -- browsepilot/backend browsepilot/browser_mcp     # 冻结检查（P4 全程）
（空输出）

$ git diff --stat ac3286d..HEAD | tail -1                                          # P4 总改动（含 1 个 P3 follow-up）
 34 files changed, 6890 insertions(+), 229 deletions(-)
```

- `backend/`、`browser_mcp/` **零改动**，冻结延续至 P5（`tests/test_architecture.py` 守卫分层；本轮无新增跨层 import）。
- P4 改动限定在 `browsepilot/eval/`、`browsepilot/tests/`、`browsepilot/docs/`、`browsepilot/src/browsepilot/agent/graph.py`（T5-follow-up 产品修复）、`browsepilot/src/browsepilot/app/executor.py`（P3 收口 follow-up `db44335`）、根 `.github/workflows/ci.yml`（T6）。
- 本任务（T9）**仅新增 docs**：`docs/v2-articles/article-4-material.md`、`docs/v2-p4-report.md`，以及 `docs/v2-articles/README.md` 的文章 4 状态行（1 行）。
- 工作树另携带会话前既有的无关未提交修改（根 `CLAUDE.md`、根 `README.md`、`browsepilot/.env.example`、`browsepilot/backend/app/config.py`）与未跟踪文件——非 P4 产出，未触碰、未提交。

## 八、无密钥声明

- 本报告与两份交付文档不含任何密钥/token；环境变量只出现名称（`BP_MODEL_API_KEY`、`BP_MODEL_BASE_URL`、`BP_DEFAULT_MODEL`、`BP_BROWSER_CHANNEL`、`BP_BROWSER_HEADLESS`）。
- 真实评测所用的 key 仅在 shell 进程内导出（值取自 `.env` 的 v1 变量映射），未写入任何文件；`data/`（含 trace 与评测报告）整体 gitignored，不随仓库提交。

## 九、提交

本任务提交仅含：

- `browsepilot/docs/v2-articles/article-4-material.md`（新建）
- `browsepilot/docs/v2-p4-report.md`（本文件，新建）
- `browsepilot/docs/v2-articles/README.md`（文章 4 状态行 + 素材文件名）

提交信息：`docs: P4 article material + delivery report`
