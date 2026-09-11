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

_待写_

## 8. 已知限制（T8 完成）

_待写_
