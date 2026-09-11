# Changelog

本文件记录 browsepilot 的所有重要变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[语义化版本](https://semver.org/lang/zh-CN/)。（2.0.0 之前没有 CHANGELOG —— V2 是一次
从零重建的重构，"旧版本"的记录见 `docs/v2-baseline.md`。）

## [2.0.0] - 2026-09-11

V2.0 用一个全新的三层实现（`src/browsepilot/`：`model/` → `agent/` → `app/`）替换了 v1 的
MCP 双进程方案（`backend/` + `browser_mcp/`），并把"能不能跑"升级为"可评测、可回归、可发布"：
评测场景集 + 一键门禁 + 真实模式量化报告。以下按 P0–P5 六个阶段归纳（每阶段的完整证据见
`docs/v2-p1-report.md` … `docs/v2-p4-report.md` 与 `docs/v2-release.md`）。

### P0 地基（2026-08-03）

- `v2.0` 分支自 `master`（`64b84fe`）创建；基线记录 `docs/v2-baseline.md`（旧套件 66 passed，
  旧代码 `backend/` + `browser_mcp/` 冻结不动，作为对照）
- `src/browsepilot/` 三层包骨架（`model/` / `agent/` / `app/`）+ 分层守卫测试
  （`tests/test_architecture.py`，禁止跨层反向 import）
- model 层：`ModelProvider` 协议 + LiteLLM 适配（统一 message/usage/cost 模型），
  结构化输出走**强制 tool calling + pydantic 校验 + 重试**（不再靠嗅探模型文本猜 JSON）
- agent 层：pydantic 状态模型、Tracer 端口 + JsonlTracer、ToolExecutor 端口（与 MCP 解耦）、
  LangGraph 骨架（classify → answer）
- app 层：`BP_` 前缀 Settings + FastAPI app 工厂 + `/health`；评测集 v0.1（scenario schema +
  3 个 P0 场景 + runner 骨架）

### P1 Tier1/2（2026-08-17 验收）

- 能力层就位：weather（内置城市表 + Open-Meteo，**无需 API key**）、search（ddgs + tenacity
  重试）、page fetch（trafilatura 正文提取，替代旧的"选择器硬编码"）
- 混合 tier 路由：规则优先 + 模型兜底（`dca74ae`），替换 v1 的一次性意图分类
- execute/answer 链 + 结构化参数 + app 层工具执行器；DI 组装根 + `/chat/stream` SSE 与错误契约
- **P0 三场景真实模式 3/3 PASS**（真实模型 + 真实网络），全量 **137 passed**；ruff / mypy /
  GitHub Actions CI 落地（`9567a90`，`7adf7c8`）

### P2 循环加固（2026-09-02 验收）

- 多轮与会话：LangGraph SqliteSaver checkpointer + `/sessions` CRUD + `/history` +
  `/chat/resume`（含匿名会话 session_id 透传修复，HITL 不再死路）
- 降级链与 HITL 重建：weather→search 策略回退 + `degradation_log` 全链路可追溯；
  interrupt/resume 语义固化（`__interrupt__` 真实 API 形状）
- 可观测性：`llm_call` trace 事件（TracingModelProvider）+ JSONL 落盘，span/parent 归属修正
- 限流 429 + 会话 token 预算闸门
- 前端迁移到 v2 API（chat/sessions/resume 全通，`npm run build` 绿）；全量 **171 passed**

### P3 Tier3（2026-09-10 验收）

- BrowserPool + 无障碍树（AX）快照 refs（≤30 边界截断），修掉并发上限/启动泄漏/幻影 ref
- 动作原语：多重定位回退（ref → role/name → CSS → text）+ 工具级重试 + JS 回退 + 参数强转
- URL 安全策略（D4/D7）：默认 https、WHATWG/UTS46 对齐的主机解析（IDNA 点变体归一 +
  IP 字面量精确匹配）、allowlist 模式、导航前把关 + **重定向后复查**
- 6 个浏览器工具接入 executor + tier3 策略链（`browser_open` → `browser_snapshot`）；
  无浏览器池时**结构化诚实降级**（不静默假成功）
- 登录/验证码检测桥接 P2 HITL（可见文本采样 + confidence 门控）；全量 **616 passed**；
  真实浏览器冒烟记录 `docs/v2-p3-smoke.md`

### P4 评测（2026-09-11 验收）

- 检查引擎 v0.3：10 种断言类型，纯函数、无 I/O；未知类型/未知 mode 一律 FAIL 且不抛异常
- 场景 schema v2 + **16 个场景**（tier1/2/3 + HITL + 多轮 + 降级；tags/mode/timeout/fragile/
  enabled/follow_up），并补了"假通过红线"合成回归网（断言在彻底失败的 outcome 上必须 FAIL）
- 真实模式 harness：BrowserPool 注入 + 逐场景 `aclose()` 隔离 + 用量/耗时/工具名采集 + 多轮接线
- 双格式（JSON/Markdown）量化报告：成功率 / P50 / P95 / token / per-tier / 跨轮方差；
  **未测指标不渲染为 0**、环境归因入报告
- 回归门禁 `eval/gate.sh`（pytest → ruff → mypy → eval mock）+ CI `Eval (mock)` 步骤；
  真实模式 acceptance 3 轮成功率 90.0%×3 完全一致、flaky 0；全量 **852 passed**

### P5 收尾（2026-09-11）

- 删除 v1 代码：`backend/`（9.9M）+ `browser_mcp/`（455K）+ `mcp_settings.json` +
  5 个引用旧模块的测试；`src/browsepilot/` 成为唯一 Python 实现
- 编排改写为 v2：`start.sh` / `start.bat`（后端 8001 + 前端 5173）、`Dockerfile` + `.dockerignore`
- 并发隔离（A2A 前置）：executor 的"当前页"改为 `ContextVar` —— 每个 asyncio 上下文各一张页，
  并发会话不再互相串页
- 性能基线：`eval/bench.py`（冷启动 / `create_app` / `/health` / tier1 mock + real）+
  `docs/v2-performance.md`
- 文档体系：`docs/v2-architecture.md`、`docs/v2-deploy.md`、`docs/v2-eval.md` 刷新；
  版本收敛（`pyproject.toml` / `uv.lock` / `browsepilot.__version__` 三者同为 `2.0.0`）+ 本 CHANGELOG

### 测试数演进（`uv run pytest tests/ -q`，同一台机器）

| 节点 | 测试数 | 出处 |
|------|--------|------|
| P0 基线（v1 旧套件） | 66 | `docs/v2-baseline.md` |
| P1 验收 | 137 | `docs/v2-p1-report.md` |
| P2 验收 | 171 | `docs/v2-p2-report.md` |
| P3 验收 | 616 | `docs/v2-p3-report.md` |
| P3 收口（`ac3286d`） | 637 | `docs/v2-eval.md` §2 |
| P3 follow-up（`db44335`） | 717 | `docs/v2-p4-report.md` §一 |
| P4 验收 | 852 | `docs/v2-p4-report.md` |
| P5-T0/T0b 修复 | 868 | `docs/v2-eval.md` §6.5 |
| P5-T1 删除 v1 测试后 | 801 | `docs/v2-eval.md` §2 P5 刷新注 |
| P5-T3 性能测试 +9（`7304bee`） | **810** | `docs/v2-eval.md` §2 / §6.1 |

### Removed

- `backend/` —— v1 FastAPI 后端（:8000）整个目录
- `browser_mcp/` —— v1 MCP 浏览器层（:9400，含 playwright MCP pool 与 11 个工具）
- `mcp_settings.json` —— 指向旧 MCP 端口的配置
- 引用旧模块的测试：`test_integration.py` / `test_multiturn.py` / `test_pre_observe.py` /
  `test_vision.py` / `test_skill_loader.py`，以及 `tests/conftest.py` 中的旧 fixture
- `pyproject.toml` 的 hatch `packages` 由 v1 的 `[backend, browser_mcp, frontend]` 收敛为
  `src/browsepilot`（旧配置会让 `uv sync/build` 失败）

### Changed

- 版本号：`pyproject.toml` / `uv.lock` / `browsepilot.__version__` 由 `0.1.0` 收敛为 `2.0.0`
  （2.0 之前三者不一致：`__init__.py` 已写 `2.0.0`，`pyproject.toml` 仍是 `0.1.0`）
- 浏览器/抓取路径不再走第二个进程（MCP server），改为进程内 BrowserPool + 工具端口

### 未实现 / 待决（有意留出，非缺陷隐瞒）

- **A2A 端口**（spec D6 预留）：`src/browsepilot/app/` 无 a2a 模块、无路由挂载；实现深度
  待用户决策（最小可用 / 完整规范 / 只留接口）
- **Tier3 交互链**：`browser_type` / `browser_click` 已实现但未编排进策略链，
  `t3_browser_interaction` 场景保持 `enabled: false`
- **评测预算门禁**：`tokens_below` / `latency_below` 断言已就绪但未进场景集；成本（USD）未采集
- **HITL 端到端**：`resume` 之后的真实浏览器链路未验证（需可达的登录墙站点）
- **CI 未在 GitHub Actions 实跑**：workflow 已就位，本地分支未推送，仅有本地 `gate.sh` 实证

完整清单与验收证据见 `docs/v2-release.md`。
