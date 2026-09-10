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

_待写_

## 7. 量化结果（T7/T8 完成）

_待写_

## 8. 已知限制（T8 完成）

_待写_
