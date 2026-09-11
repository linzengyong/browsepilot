# BrowsePilot V2 评测体系

> 本文档在 P4 阶段完成（T8）。当前为 T0 骨架：环境与基线记录。

## 1. 环境与运行前提

| 项 | 值 | 说明 |
|----|----|------|
| Python | 3.14（`uv run`） | `uv run python -m eval.run_eval` |
| 模型 key | `BP_MODEL_API_KEY`（或 `OPENAI_API_KEY` 回退） | 真实模式必需；仅进程内，不入库 |
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

**P3 → P4 交接要点**（来自 P3 final review + 收口修复）：
- tier3 策略链 = `[browser_open, browser_snapshot]`；type/click 未入链（场景需标 `enabled: false`）
- 无导航的观察 / 只导航未观察 → `stop_reason="partial"`；全策略失败仍报 `completed`（既有行为）
- 真实模式 harness 已注入 BrowserPool + 逐场景 `aclose()` 隔离（`ac3286d`）
- answer 节点已读 `human_response`（resume 后不再复述"请手动登录"）

## 3. 评测哲学（T8 完成）

_待写_

## 4. 场景规范（YAML schema）（T8 完成）

_待写_

## 5. 检查类型表（T8 完成）

_待写_

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

### 6.2 CI 跑什么 / 不跑什么（P4-D8）

`.github/workflows/ci.yml`（触发：push `master`/`v2.0` + PR；全部 `working-directory: browsepilot`）：

```
Install（uv sync --all-extras --frozen）→ Lint → Types → Tests → Eval (mock)
```

- **跑**：mock 模式（`uv run python -m eval.run_eval --mock`）—— 只依赖 fake deps，无网络可复现。
- **不跑**：真实模式。CI 无网络、无模型 key、无浏览器；且真实结果受 ddgs 结果漂移影响会 flaky，把 flaky 接进必过门禁等于把门禁变成噪声源（P4-D8）。真实模式为手动/验收运行（可选定时任务），**绝不进 CI**。

### 6.3 真实模式运行前提

与 §1 的环境表同源；这里是跑 acceptance 的最小前提：

| 前提 | 值 | 为什么 |
|------|----|--------|
| 模型 | `BP_DEFAULT_MODEL=deepseek/deepseek-chat` | 思考模型（deepseek-flash / v4-pro）拒绝 forced tool_choice → structured 调用报错；显式钉住非思考模型 |
| 模型 key | `BP_MODEL_API_KEY` | 仅进程内环境变量（不入库）；留空时回退 LiteLLM 的 provider 环境变量 |
| 浏览器（tier3） | `BP_BROWSER_CHANNEL=msedge`，或 `uv run playwright install chromium` | 本机 Playwright 需 chromium rev 1217、缓存为 1208 → 默认启动失败；池起不来时报告环境行写 `unavailable (start failed)`，tier3 的失败**在报告里**归因到环境而非任务 |
| HITL 场景 | `BP_BROWSER_HEADLESS=false` | 人工介入需要可见窗口 |

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

### 7.7 数据可信度限制（读这节数字前先读这里）

- **token 是 chat-only 下限**：`usage_source=chat_only`——`structured`/`stream` 调用没有 usage 面，不进总和。3 轮样本池里 **30/63 次模型调用有计量（47.6%）**，所以 10191 是**下限**而不是"总消耗"（报告里就是这个口径，每场景还带 `measured_calls/model_calls`）。
- **成本未采集**：3 轮 `cost_samples=0`、`total_cost_usd=null`。每次运行启动时 LiteLLM 取远端价格表失败（log：`Failed to fetch remote model cost map from https://raw.githubusercontent.com/… : timed out. Falling back to local backup`），本地备份也没给出该模型的成本 → 写"未采集"，不估算。
- **1 条场景天然没有 token 数**：`t2_read_page_failure` 3 轮 token 均缺席——它走模板答案路径（`token_usage=null`，0 次模型调用），是"未知不是 0"的如实处理（所以每轮 `token_samples=9/10`），不是采集故障。
- **延迟读数含依赖网络时间**：P95 跨轮 20053 → 33315 ms（stdev 5514.86），单条极值 45503.05 ms（R3 `t2_search_structural`）；这把 P95 变成"依赖延迟 + 模型时间"的混合读数，引用时别只贴 P95。
- **样本量**：3 轮 × 10 场景 = 30 次场景运行（每场景 3 次）。这是**同一场景集重复跑**的波动，不是跨模型 / 跨日 / 跨网络的抽样；换模型（或换网络出口）结论不保证迁移。
- **mock 数据不代表模型质量**：CI 门禁跑 FakeProvider/FakeExecutor（§6.2），只能证明断言与管线可复现；本节所有数字都是真实模式，二者不可混用。
- **p0-search 归档与本节的关系**：`p0-search` 已归档（`enabled: false`），其内容断言的当前形态就是 `t2_search_content`（同 prompt、同 keywords、同样 fragile）；跨阶段对照（P1 通过 → P2 漂移 → P4 结构化）见 P4 报告 / 文章 4 素材。

## 8. 已知限制（T8 完成）

_待写_
