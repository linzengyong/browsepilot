# BrowsePilot V2.0 发布报告（P5 Task 5）

- 版本：**2.0.0** —— `pyproject.toml` / `uv.lock` / `browsepilot.__version__` 三点一致
  （发布前三者不一致：`__init__.py` 已是 `2.0.0`，`pyproject.toml` 与 `uv.lock` 仍是 `0.1.0`）
- 分支：`v2.0`（自 `master` = `64b84fe` 起 **90 个提交**）；发布提交与报告、CHANGELOG 同一次提交
  （`release: V2.0.0`）。**不推送远程、不打 git tag**（计划 Global Constraints：本地发布）
- 验收日期：2026-09-11（本地 21:32–21:39，UTC 13:32–13:39）
- 发布判据（全部实测，见 §一）：`eval/gate.sh` **ALL PASS**；真实模式应用层冒烟 tier1 + tier3 全绿；
  评测 harness 真实模式 smoke `passed=6 failed=0`（exit 0）、acceptance 成功率 **90.0% (9/10)**
  （唯一非门禁失败 = 已标记 `fragile` 的 ddgs 内容漂移）；前端 dev-server 首页 200 + 代理 SSE 会话到
  `done`；全量 **810 passed**
- 一句话结论：**V2.0 的核心替换（`src/browsepilot/` 三层实现取代 v1 的 `backend/` + `browser_mcp/`
  双进程 MCP 方案）与评测体系已交付并验证**；A2A 端口按 spec D6 保持"预留未实现"，实现深度待用户决策。
  已知限制与待决项见 §四。

---

## 一、发布验收证据（P5-T5 实测）

工作目录 `D:/AI_Agent_Demo/browsepilot`，`PYTHONIOENCODING=utf-8`。

真实模式环境：`BP_DEFAULT_MODEL=deepseek/deepseek-chat`、`BP_BROWSER_CHANNEL=msedge`、
`BP_BROWSER_HEADLESS=true`（应用冒烟）/ 场景自带（评测）、`BP_MODEL_API_KEY ← OPENAI_API_KEY`、
`BP_MODEL_BASE_URL ← OPENAI_BASE_URL`（`api.deepseek.com/v1`；仅进程内环境变量，未落盘）。

### 1.1 门禁 `bash eval/gate.sh` → ALL PASS

运行时机：**发布工作树**（版本号已收敛 + CHANGELOG 已在；`head=e55efe1` + 工作区改动），
原始日志 `data/release/p5-t5-gate.log`（`data/` 整体 gitignored，不提交）。

```text
[gate] browsepilot eval gate (mock mode)
[gate] env: cwd=/d/AI_Agent_Demo/browsepilot branch=v2.0 head=e55efe1 time=2026-09-11 21:32:54
[gate] toolchain: uv 0.10.4 (079e3fd05 2026-02-17)
[gate] >>> pytest tests/ -q                 → 810 passed in 15.93s
[gate] >>> ruff check src/ eval/ tests/     → All checks passed!
[gate] >>> mypy src/browsepilot/            → Success: no issues found in 32 source files
[gate] >>> eval run_eval --mock             → passed=8 failed=0 fragile_failed=0 skipped=4 disabled=4 total=16
[gate] ALL PASS
```

`exit 0`。mock 口径与 P4 一致：4 SKIP = real-only（2 条 tier3 + HITL + 多轮），4 DISABLED =
3 条 `p0_*` 归档 + `t3_browser_interaction`（交互链未入图）。

### 1.2 真实模式应用层冒烟（起服务 → SSE 到 done → 关停）

```bash
uv run uvicorn browsepilot.app.main:app --port 8001     # BrowserPool started (headless=True, channel=msedge, max_pages=3)
curl http://127.0.0.1:8001/health
curl -N -X POST http://127.0.0.1:8001/chat/stream -H "Content-Type: application/json" --data-binary @req.json
```

| 步骤 | 结果 |
|------|------|
| `/health` | **200**，`{"status":"ok","version":"2.0.0","model":"deepseek/deepseek-chat"}`（6.0 ms；版本号来自 app 元数据，证明发布版本在运行） |
| tier1 任务（`北京今天天气怎么样？`） | **200**，10.3 s，SSE 三事件 `phase/start → answer → done`；`stop_reason=completed`，`result_data={"city":"北京","temperature_c":24.1,"conditions":"晴","humidity":54,...}` |
| tier3 任务（`用浏览器看看 example.com 页面上有什么内容`） | **200**，4.2 s，同样三事件到 `done`；`result_data.action="snapshot"`、`opened_url="https://example.com/"`、`navigated=true`；答案含真实页面内容（`Example Domain` / IANA 链接） |
| 事件日志 | 无 ERROR/Traceback；一条 LiteLLM `WARNING`（远端价格表拉取超时 → 回退本地备份，不影响任务）与一条 `DEBUG`（navigate 的 `networkidle` 500 ms 有界等待，预期行为） |
| 关停 | `netstat` 定位 PID → `taskkill /T /F`，端口 8001 已释放 |

Trace 证据（`data/traces/2c4e85ad-d84b-4317-8387-1c6cae0f63d2.jsonl`，21 事件，13:33:45→13:34:02 UTC）：

```text
tier1: decision{intent=weather,tier=tier1,conf=1.0} → execute(tool_call weather status=success, 9597 ms) → answer(725 ms)
tier3: decision{intent=browser_task,tier=tier3,conf=1.0}
       → execute(tool_call browser_open status=success ; tool_call browser_snapshot status=success, 2446 ms) → answer(1734 ms)
```

→ tier3 的"打开 + 观察"链在真实浏览器下按顺序执行且都成功，答案引用了真实页面内容（非失败文案回显）。

### 1.3 评测 harness 真实模式（与 P4 同一把尺子）

命令与结果（`--json/--md` 产物在 `data/release/`，gitignored）：

| 运行 | 命令 | 结果 |
|------|------|------|
| 冒烟 | `uv run python -m eval.run_eval --tag smoke --repeat 1` | `passed=6 failed=0 fragile_failed=0 skipped=6 disabled=4`，**exit 0**（6/6 全过：t1×3 + t2 read_page/failure/search_structural） |
| 验收 | `uv run python -m eval.run_eval --tag acceptance --repeat 1` | `executed=10 passed=9 failed=0 fragile_failed=1 skipped=2 disabled=4`，成功率 **90.0% (9/10)**，**exit 0**；P50 **3808.0 ms** / P95 **32284.9 ms**；tokens **3339**（9 条样本，下限口径）；`cost_usd=null`（已知限制 §四-4） |

- acceptance 仅有的失败 = **`t2_search_content`（`fragile: true`，不计入门禁）**：ddgs 结果内容漂移
  导致关键词落空 —— 与 P4 三轮的同一条、同因（P2 D1），**非 V2.0 代码回归**。
- 分层：tier1 5/6（含该 fragile）、tier2 2/2、tier3 **2/2**（`t3_browser_open` + `t3_browser_snapshot`，
  断言 `tools_called=[browser_open, browser_snapshot]`、证据词 `Example Domain`、`degradation_absent` 全过）。
- 与 P4 收口（`--tag acceptance --repeat 3`，90.0%×3）读数一致；本轮为发布 HEAD 的单轮复核。

### 1.4 前端手测（dev-server + 代理 SSE 会话）

```bash
cd frontend-vue && npm run dev      # VITE v6.4.2 ready in 402 ms → http://localhost:5173/
curl -o /dev/null -w '%{http_code}' http://localhost:5173/            # 200（404 B：BrowsePilot index.html）
curl -o /dev/null -w '%{http_code}' http://localhost:5173/src/main.ts # 200（1136 B，模块可编译）
```

| 检查 | 结果 |
|------|------|
| 首页 `/` | **200**，`<title>BrowsePilot</title>`、`#app` 挂载点、`/@vite/client` 注入正常 |
| 入口模块 `/src/main.ts` | **200**（未被编译器拦下） |
| 代理 `/api/health`（Vite → :8001） | **200**，`{"status":"ok","version":"2.0.0",...}` —— 前端到 v2 后端的接线在发布 HEAD 下可用 |
| 经代理的 SSE 会话 `/api/chat/stream` | **200**，9.5 s，`phase/start → answer → done`（真实天气数据），即"前端发起的一轮真实对话"链路通 |

未做完整 UI 交互（点击/多轮手测）：本次为命令行验证；浏览器内的界面行为属于人工验收，未在本次执行
（见 §四-新增 4）。

### 1.5 测试数

- `uv run pytest tests/ -q` → **810 passed**（门禁内实测，15.93 s）
- 演进轨迹见 `CHANGELOG.md`「测试数演进」表（66 → 137 → 171 → 616 → 637 → 717 → 852 → 868 →
  801 → **810**），每格标注出处文档

---

## 二、交付清单（V2.0 实际状态与路径）

### 2.1 代码（唯一 Python 实现：`browsepilot/src/browsepilot/`，32 个模块）

| 层 | 模块 | 内容 |
|----|------|------|
| `model/` | `provider.py` `litellm_provider.py` `structured.py` `messages.py` | `ModelProvider` 端口 + LiteLLM 适配；强制 tool calling 的结构化输出 + pydantic 校验 + 重试；统一 message/usage/cost 模型 |
| `agent/` | `graph.py` `state.py` `router.py` `params.py` `tools.py` `tracer.py` `tracer_provider.py` `api.py` | LangGraph 编排（classify→execute→answer）、pydantic 状态、混合 tier 路由、结构化参数、ToolExecutor 端口、Tracer 端口 + JSONL sink、`llm_call` 事件桥 |
| `app/` | `main.py` `config.py` `executor.py` `sessions.py` `rate_limit.py` `services/` `browser/` | FastAPI 组装根（`/health` `/sessions` `/history` `/chat/stream` `/chat/resume`）+ lifespan（checkpointer / 浏览器池）；`BP_` 前缀 Settings；工具执行器（含浏览器工具 + 降级链）；会话存储；限流与 token 预算；weather/search/fetch 服务；BrowserPool / 动作原语 / URL 安全策略 / AX 快照 / HITL 检测 |

### 2.2 测试（`browsepilot/tests/`，28 个测试文件，810 条）

`test_model/`（3）· `test_agent/`（7，含 router/graph/tracer）· `test_app/`（12，含浏览器相关 5 个）·
`test_eval/`（5）· `test_architecture.py`（分层守卫：三层单向依赖 + `app.browser` 自包含）。
**套件不依赖真实模型/浏览器/网络**：真实依赖只在评测 harness 与本次冒烟中出现。

### 2.3 评测（`browsepilot/eval/`）

| 文件 | 说明 |
|------|------|
| `scenarios.py` + `scenarios/*.yaml`（**16 条**） | 场景 schema v2（tags/mode/timeout/fragile/enabled/follow_up），覆盖 tier1/2/3 + HITL + 多轮 + 降级 |
| `checks.py`（v0.3，10 种断言） | 纯函数、无 I/O；未知类型/mode → FAIL 不抛异常 |
| `run_eval.py` | mock/real 双模式 runner；逐场景隔离、重试与失败归因 |
| `harness.py` | 真实依赖注入（BrowserPool + 逐场景 `aclose()`）、用量/耗时/工具名采集、多轮接线 |
| `report.py` | JSON + Markdown 双格式量化报告（成功率 / P50 / P95 / token / per-tier / 跨轮方差） |
| `fake_deps.py` | FakeProvider / FakeExecutor —— 无网络、无 key、无浏览器即可回归 |
| `gate.sh` | 一键门禁（pytest → ruff → mypy → eval mock），本地与 CI 同口径 |
| `bench.py` | 性能测量（冷启动 / `create_app` / `/health` / tier1 mock+real），不进 gate |

### 2.4 文档（`browsepilot/docs/`）

| 文件 | 行数 | 状态 |
|------|------|------|
| `v2-architecture.md` | 270 | ✅ 三层架构 + 请求生命周期 + HITL/降级/Tracer 机制 + D1–D8 决策 |
| `v2-deploy.md` | 237 | ✅ `BP_*` 环境变量全表 + Docker / 本地启停 + 真实模式前提 + 门禁读数 |
| `v2-eval.md` | 537 | ✅ 评测哲学 / 场景规范 / 断言类型表 / 运行指南 / 真实运行记录 / 限制 |
| `v2-performance.md` | 175 | ✅ 冷启动 / health / 端到端延迟读数（含环境与口径） |
| `v2-baseline.md` | 17 | ✅ P0 基线（v1 代码与 66 测试留下的 before 快照） |
| `v2-p1-report.md` / `v2-p2-report.md` / `v2-p3-report.md` / `v2-p4-report.md` | 105/127/222/173 | ✅ 各阶段交付报告（验收证据 + 偏差 + 遗留） |
| `v2-p3-smoke.md` / `v2-p1-validation.md` | 423/111 | ✅ 真实浏览器冒烟记录 / P1 前置验证 |
| `v2-release.md`（本文件） | — | ✅ 发布报告 |
| 根 `README.md` | — | ⚠️ **未更新**：仍是 v1 描述（`backend/app/` :8000、`browser_mcp/` :9400）。P5-T4 Step 3 需用户裁决（工作树携带会话前的未提交改动），见 §四-新增 1 |

### 2.5 文章素材（5 篇，`browsepilot/docs/v2-articles/`）

| # | 素材文件 | 行数 | 素材状态（`v2-articles/README.md` 口径） |
|---|----------|------|------------------------------------------|
| 1 | `article-1-material.md` | 106 | 起草中（P0 "before" 对比数据已采） |
| 2 | `article-2-notes.md` | 176 | P1 实战案例已补 |
| 3 | `article-3-material.md` | 308 | 起草中；P3 补 tier3 抢匹配回归故事 |
| 4 | `article-4-material.md` | 455 | P4 素材已就绪（两个真实案例：假通过红线 / 评测→修复闭环） |
| 5 | `article-5-material.md` | 460 | P2 素材 + P3 Tier3 trace / HITL 事件链 |

> 用户主笔的成稿在**仓库根 `docs/博客/`（该目录被根 `.gitignore` 整体忽略，不进版本控制）**：
> 文章 1（36 行）、文章 2（82 行）已有正文，文章 3/4/5 目前是空占位文件。素材 vs 成稿的差异属正常
> 分工（素材目录是技术素材，成稿由用户写）。

### 2.6 编排与部署

`start.sh` / `start.bat`（v2：后端 8001 + 前端 5173）· `Dockerfile` + `.dockerignore`（只 COPY
`src/eval/frontend-vue` + `pyproject/uv.lock`；CMD → `browsepilot.app.main:app`）·
`docker-compose.yml` · `.github/workflows/ci.yml`（Install → Lint → Types → Tests → Eval (mock)）。

---

## 三、版本内容摘要与 spec §5 P5 对照

### 3.1 spec §5「P5 收尾」行（"A2A 端口 · 文档 · 性能 · V2.0 发布"）对照

| spec §5 要求 | 状态 | 证据 |
|--------------|------|------|
| A2A 端口 | ⏸️ **待决（未实现）** | `src/browsepilot/app/` 无 a2a 模块、无路由挂载；spec D6 为"预留接口设计，实现放后期"；深度需用户三选一（最小可用 / 完整规范 / 只留接口）。**这是 V2.0 唯一的未实现交付项** |
| 文档 | ✅ 完成（1 项待裁决） | `v2-architecture.md` / `v2-deploy.md` / `v2-eval.md` 刷新 + 本报告；根 `README.md` 未更新（待用户裁决，§四-新增 1） |
| 性能 | ✅ 完成 | `docs/v2-performance.md` + `eval/bench.py`（冷启动 / `create_app` / `/health` / tier1 mock+real，n≥3–5，方法与环境完整） |
| V2.0 发布 | ✅ 完成 | 版本号三点收敛为 `2.0.0` + `CHANGELOG.md` + 本报告 + 门禁/冒烟/评测实测证据 |

### 3.2 五阶段摘要（详版见 `CHANGELOG.md`）

| 阶段 | 主题 | 主要交付 | 测试数 | 报告 |
|------|------|----------|--------|------|
| P0 | 地基 | 三层骨架 + 分层守卫；model 层（结构化输出）；agent 骨架（Tracer / ToolExecutor / LangGraph）；评测集 v0.1；v1 代码冻结 | 66（基线） | `v2-baseline.md` |
| P1 | Tier1/2 | weather / search / fetch 服务；混合 tier 路由；execute→answer 链；`/chat/stream` SSE；ruff+mypy+CI | 137 | `v2-p1-report.md` |
| P2 | 循环加固 | checkpointer + 会话 API + HITL resume；降级链（weather→search）与 degradation_log；`llm_call` trace；限流与预算；前端迁移 v2 API | 171 | `v2-p2-report.md` |
| P3 | Tier3 | BrowserPool + AX 快照 refs；多重定位回退 + 重试 + JS 回退；URL 安全策略（WHATWG/UTS46）；6 个浏览器工具进 executor + tier3 策略链；HITL 检测桥接 | 616 | `v2-p3-report.md` |
| P4 | 评测 | 检查引擎 v0.3（10 断言）；16 条场景集；真实模式 harness；双格式量化报告；`gate.sh` + CI；真实 3 轮（90.0%×3，flaky 0） | 852 | `v2-p4-report.md` |
| P5 | 收尾 | 删除 v1 代码（`backend/` + `browser_mcp/`）；编排改写；并发页隔离（A2A 前置）；性能基线；文档体系；**V2.0.0 发布** | 810 | 本报告 |

---

## 四、已知限制（carry-over：P4 §六 14 项 + P5 新增）

> 状态口径：**P5 已修** = 本次发布前解决；**未变** = 原样带入 V2.0.0 发布（不是缺陷隐瞒，是范围外/待决策）。

### 4.1 P4 报告 §六 遗留清单逐项状态

| # | 项（P4 编号） | P5 处置 |
|---|---------------|---------|
| 1 | **tier3 交互链未接入**（`browser_type` / `browser_click` 未编排进 `_STRATEGY_CHAIN`） | 未变：`t3_browser_interaction` 仍 `enabled: false`，type/click 后的采样/重定向复查无场景覆盖 |
| 2 | **多会话共享 current page** | **已修（可复现证据）**：`352c58a` 将"当前页"改为 `ContextVar`，每个 asyncio 上下文各一张页；残余：`executor.aclose()` 未接 app lifespan、上下文结束不自动归还页（长跑有页 churn） |
| 3 | `tokens_below` / `latency_below` 无场景声明 | 未变：引擎与单测就绪，预算门禁未进场景集 |
| 4 | `cost_usd` / 成本统计未采集 | 未变：本轮 acceptance 仍 `cost_samples=0`、`total_cost_usd=null`（价格表超时） |
| 5 | **HITL 链路未端到端验证** | 未变（部分缓解）：场景打 `hitl-live` tag 从门禁移出（`6fd161a`），`needs_human` 既非通过也非已确认缺陷；`resume` 后真实浏览器链路仍未测 |
| 6 | multi_turn 架构缺口（全败路径不注入 history；classify 只看任务原文） | 未变：`multi_turn_context` 场景不断言 `tools_called`（避免必然抖动） |
| 7 | router 强词问题式短语（"为什么京东比淘宝便宜" → tier3） | 未变：规则调优待评测数据说话 |
| 8 | P3 §八 其余 Minor（`waited` 恒 False、JS 回退成功语义、无 press/submit、`scroll(inf)` OverflowError、redirect-then-navigate-failure 不复查等） | 未变 |
| 9 | 报告不存 answer 全文（失败取证只能靠 ledger/探针日志） | 未变 |
| 10 | 兜底成功后 `stop_reason` 仍 `completed`（无法从 stop_reason 区分降级成功） | 未变（spec 只要求"带说明"，降级来源已由 `25b1c76` 在答案里声明） |
| 11 | t3 证据关键词依赖模型引用页面标题（全中文改写可能假 FAIL） | 未变：本轮 tier3 两条真实运行均 PASS |
| 12 | `mypy eval/` 有 16 个既有错误（`run_eval.py:242` 的 `**dict` 解包） | 未变：gate/CI 只查 `src/browsepilot/` |
| 13 | **GH Actions 未实际执行**（本地分支不推送） | 未变：`6fd161a` 已在文档中如实记录为已知缺口；本轮以 `gate.sh` 本地实测代替 |
| 14 | 跨模型 / 跨网络稳定性未采集（3 轮 = 同场景集重复跑，非抽样） | 未变 |

### 4.2 P5 新增

| # | 项 | 影响 | 处置 |
|---|----|------|------|
| 1 | **根 `README.md` / `CLAUDE.md` 仍是 v1 描述**（README：`backend/app/` :8000、`browser_mcp/` :9400、目录树含已删除目录；`CLAUDE.md`：`backend/app/` 七节点图、`browser_mcp` 模块、":9400 MCP 服务器"、`OPENAI_*` 环境变量 —— 同样未随 V2 更新） | 新同事按 README / `CLAUDE.md` 起步会走错路径；`browsepilot/docs/v2-deploy.md` 是当前的正确入口 | P5-T4 Step 3 明确需要用户裁决（工作树携带会话前的 `CLAUDE.md` / `README.md` / `.env.example` 未提交改动）；本次未触碰根文件 —— **发布后第一优先跟进项**（README 与 `CLAUDE.md` 并入同一跟进项） |
| 2 | 无 git tag、未推送远程 | 无法用 tag 锚定 V2.0.0，CI 仍未跑过真实 runner（同 4.1-13） | 计划明确"不推送"；如需 tag，一行命令即可（由用户决定） |
| 3 | A2A 端口未实现 | 外部 agent 无法按标准协议调用（spec D6 的承诺尚未兑现） | 待用户决策实现深度（§三 3.1） |
| 4 | 前端未做浏览器内完整 UI 手测 | 界面交互（多轮、会话切换、流式渲染）未经人工确认 | 本次仅命令行验证首页/模块/代理/SSE；真实浏览器 UI 手测属人工验收 |
| 5 | `uv.lock` 随版本号机械更新（1 行） | 无 —— 这是必需的伴随改动，否则 CI 的 `uv sync --frozen` 与 `pyproject.toml` 不一致 | 已随发布提交；`uv lock --check` 通过（182 packages；同日交付前依赖收敛后为 **120 packages** —— 删掉零 import 的直接依赖、补声明 `aiosqlite`） |

---

## 五、spec §6「明确不做」逐条确认未做

| spec §6 条目 | 是否做了 | 证据 |
|--------------|----------|------|
| 前端重建（Vue3 保持，只修必要 bug） | **未做**（保持 Vue 3） | P5 对 `frontend-vue/` 零改动（`git diff 3d51612..HEAD -- frontend-vue` 为空）；整分支改动为 P2 的 v2 API 接线（172+/392−），无框架级重建 |
| 本地 trace 可视化页面（先只有 JSONL） | **未做** | 观测输出只有 JSONL（`data/traces/*.jsonl`）；前端 `ReplayPanel.vue` 顶部注释明确"V2 后端未提供 `/replay` 端点……此面板降级为静态说明" |
| 自研评测平台 UI | **未做** | 评测只有 CLI（`run_eval.py` / `bench.py`）+ JSON/Markdown 报告，无 Web UI |
| 多 Agent 数据源（pi / Claude Code 会话等） | **未做** | Tracer 事件模型按格式无关设计（种子），但无任何第二数据源接入（`src/browsepilot/agent/tracer*.py` 只服务本 agent） |
| pi-insights 项目相关工作 | **未做** | 仓库内无相关代码/文档引用 |

> 另计划 §"P5 明确不做"中的 4 项（站点专用适配器、多页/多标签管理、可视化回放 UI、部署 K8s/多实例）
> 同样确认未做：部署文档只给单实例 Docker + 本地。

---

## 六、范围与冻结确认

- 本任务（P5-T5）改动仅 **4 个文件**：`pyproject.toml`（版本）、`uv.lock`（版本机械同步）、
  `CHANGELOG.md`（新建）、`docs/v2-release.md`（新建）—— 与 §八 的提交清单一致。
  （`src/browsepilot/__init__.py` **未改** —— 已是 `2.0.0`，不属本次改动。）
- 未触碰：根 `README.md` / `CLAUDE.md` / `.env.example` / `skills/` / `frontend-vue/` / `eval/` /
  `tests/` / `src/browsepilot/` 业务代码。
- v1 代码冻结在 P5-T1 结束：`backend/`、`browser_mcp/`、`mcp_settings.json` 及 5 个引用旧模块的测试
  已删除（`07bc3bd` `33aadad`）；`src/browsepilot/` 是唯一 Python 实现。
- 工作树仍携带**会话前既有**的未提交改动（根 `CLAUDE.md`、根 `README.md`、`browsepilot/.env.example`）
  与未跟踪文件 —— 非 P5 产出，未触碰、未提交。

## 七、无密钥声明

本报告、`CHANGELOG.md` 与门禁/冒烟日志均不含任何密钥或 token；环境变量只出现名称
（`BP_MODEL_API_KEY`、`BP_MODEL_BASE_URL`、`BP_DEFAULT_MODEL`、`BP_BROWSER_CHANNEL`、
`BP_BROWSER_HEADLESS`、`BP_NAVIGATION_POLICY`）。真实运行所用 key 仅在 shell 进程内导出
（值取自 `.env`，`.env` 本身 gitignored），未写入任何文件；`data/`（trace / 评测报告 / 冒烟日志）
整体 gitignored，不随仓库提交。

## 八、提交

```text
release: V2.0.0
  browsepilot/pyproject.toml        0.1.0 → 2.0.0
  browsepilot/uv.lock               version = 2.0.0（机械同步，保证 uv sync --frozen 一致）
  browsepilot/CHANGELOG.md          新建（Keep a Changelog，V2.0.0 条目 + 测试数演进 + 移除/待决）
  browsepilot/docs/v2-release.md    新建（本文件）
```

`git add` 仅上述 4 个文件；无 tag、无推送。
