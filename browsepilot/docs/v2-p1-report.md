# V2.0 P1 交付报告（P1 Task 12 验收）

- 分支：`v2.0`（自 master 创建，master HEAD = 64b84fe，P1 期间 master 未变）
- 验收日期：2026-08-17（本地时间，UTC 2026-08-16）
- 验收结论：✅ **P0 三场景真实模式 3/3 PASS**；全量测试 137 passed；E2E 冒烟三事件链路正常；旧代码冻结 0 改动

## 一、全量测试（Step 1）

`uv run pytest tests/ -q` → **137 passed, 1 warning in 9.37s**

- 组成：旧基线 66（P0 Task 1 记录）+ P0/P1 新增 71
- warning：`backend/app/config.py:19` PydanticDeprecatedSince20（旧代码冻结区，非失败，不修）

工程化门禁复验（T10）：

```bash
uv run ruff check src/ eval/ tests/      → All checks passed!
uv run ruff format --check src/ eval/ tests/ → 49 files already formatted
uv run mypy src/browsepilot/             → Success: no issues found in 23 source files
```

## 二、评测真实模式验收（Step 2）—— P1 验收标尺

命令：`uv run python -m eval.run_eval`（真实模型 deepseek/deepseek-chat + Open-Meteo/ddgs 真实网络）

| 场景 | 结果 | detail | 耗时（trace 事件） |
|---|---|---|---|
| p0-weather | ✅ PASS | `city='北京' expected='北京'; temperature_c=23.2 (number)` | weather 工具 2.87s + 回答 2.43s ≈ 5.3s |
| p0-search | ✅ PASS | `keywords hit=['2024']` | search 工具 4.22s + 回答 0.96s ≈ 5.2s |
| p0-read-page | ✅ PASS | `keywords hit=['Python']` | fetch 工具 6.75s + 回答 2.36s ≈ 9.1s |

输出汇总：`[eval] 3/3 passed`；全程 19.6s（trace 首末事件时间戳 16:03:34.775 → 16:03:54.395 UTC）。

- 凭据接线：`.env` 仅含 v1 的 `OPENAI_API_KEY`/`OPENAI_BASE_URL`（api.deepseek.com）；验收 shell 内导出为 `BP_MODEL_API_KEY`/`BP_MODEL_BASE_URL`（仅进程内，未写文件、未提交）。
- 注：终端回显的 city 字段为 Windows 控制台编码显示乱码，检查逻辑为 Python 字符串相等且判定 PASS，非数据问题（trace 内 UTF-8 字节正确）。
- trace 落盘：`browsepilot/data/traces/6a8f12d9-145e-4543-9f82-ddb7da16f2e6.jsonl`（gitignored，不提交）。

## 三、E2E 冒烟（Step 3，真实服务）

启动：`uv run uvicorn browsepilot.app.main:app --port 8001`（同 env 导出），健康探测通过后执行。

`GET /health`：

```
{"status":"ok","version":"2.0.0","model":"deepseek/deepseek-chat"}
```

`POST /chat/stream`（`--data-binary @data/task12.json`，规避 Windows/MSYS2 curl 中文编码问题——T8 经验教训），SSE 三事件按契约顺序：

```
data: {"type": "phase", "phase": "start", "task": "北京今天天气怎么样？"}

data: {"type": "answer", "text": "北京今天晴，气温23.2°C，湿度77%，风速1.7公里/小时。"}

data: {"type": "done", "stop_reason": "completed", "result_data": {"city": "北京", "temperature_c": 23.2, "conditions": "晴", "humidity": 77, "wind_speed_kmh": 1.7}}
```

- 服务日志：`GET /health HTTP/1.1 200 OK`、`POST /chat/stream HTTP/1.1 200 OK`
- 收尾：`taskkill /PID 32268 /T /F`（通过 netstat 定位监听 8001 的 Windows PID）；任务完成后 8001 端口已释放，临时任务文件已删除。

## 四、旧代码冻结（Step 4）

```bash
git diff master..v2.0 --stat -- browsepilot/backend browsepilot/browser_mcp browsepilot/frontend-vue
```

→ 空输出（0 文件）。P1 改动仅限 `browsepilot/src/`、`browsepilot/eval/`、`browsepilot/tests/`、`browsepilot/docs/`、`browsepilot/pyproject.toml`、`browsepilot/uv.lock`、`browsepilot/Dockerfile`、`browsepilot/docker-compose.yml` 及仓库根 `.github/workflows/ci.yml`。

## 五、P1 相对设计的偏差与理由（SDD ledger）

| # | 任务 | 偏差 | 理由/决议 | 提交 |
|---|---|---|---|---|
| 1 | T4 | `search.py` 增加 `reraise=True` | 评审发现 RetryError 双重包装 + 浪费一次重试（Important） | 480fc35 |
| 2 | T5 | 测试 fixture 增强（trafilatura `MIN_CONTENT_LENGTH=100` 基线救援） | 控制器批准 Option A；归属注释随后更正 | c00c40b / 25dc811 |
| 3 | T7 | 修复计划真实 bug：`await provider.chat(...).text` 优先级错误 → `(await ...).text`；补 2 个图测试 | 计划与代码证据冲突，按证据修正 | 5e17fb9 |
| 4 | T8 | SSE 错误处理 + 更强流式断言（2 个 Important 发现） | 评审发现 | 682fabd |
| 5 | T9 | `test_run_eval_smoke` 切换 `--mock` | 避免 CI 外网/凭据依赖，保持确定性 | aaef3ab |
| 6 | T10 | CI workflow 从 `browsepilot/.github/` 移至仓库根 `.github/workflows` + `working-directory` | GitHub Actions 仅发现仓库根 workflow | 7adf7c8 |
| 7 | T11 | article-3 规则顺序示例更正（事实性错误） | 按证据修正 | 5ddb09f |

## 六、P1 交付检查单

- [x] Tier1 天气：城市表 + Open-Meteo，`p0-weather` 评测 PASS（查天气 100% 可靠，不碰浏览器）
- [x] Tier1 搜索：ddgs + tenacity 重试，`p0-search` 评测 PASS
- [x] Tier2 网页阅读：trafilatura 正文提取，`p0-read-page` 评测 PASS
- [x] 意图路由：规则→模型→兜底 混合策略（文章 3 素材）
- [x] 重试统一 tenacity（structured/search/fetch 三处）
- [x] `/chat/stream` SSE 可用，OpenAPI 文档清晰（产品契约）
- [x] eval runner mock/real 双模式，CI 可跑
- [x] ruff/mypy 全绿 + GitHub Actions + Dockerfile/compose 落地
- [x] 架构守卫绿；旧代码（backend/browser_mcp/frontend-vue）零改动
- [x] 文章 2 实战案例、文章 3 素材产出
- [x] 降级行为符合 P1 验收：失败时明确告知原因，不答非所问

## 七、遗留问题与观察

1. **ddgs 网络稳定性/延迟**：P1 前置验证实测冷启动 21.36s、复用连接 6.61s；本次验收 search 工具 4.22s。ddgs 为外部依赖，间歇慢/超时时由 tenacity 重试兜底；建议 P2 观察是否需要备用搜索源或结果缓存。
2. **凭据接线**：V2 `Settings` 只读 `BP_*` 前缀，`.env` 仍只有 v1 变量。真实模式评测/冒烟需在 shell 导出 `BP_MODEL_API_KEY`/`BP_MODEL_BASE_URL`（不写文件）。后续可考虑在 `.env` 增加 `BP_*` 段（不入库）。
3. **trace 文件位置**：每次运行落 `browsepilot/data/traces/<trace_id>.jsonl`（gitignored）。本次验收 trace：`6a8f12d9-145e-4543-9f82-ddb7da16f2e6.jsonl`。
4. 仓库既有未跟踪文件（`.pi/`、`AI_Agent_Demo.zip`、`docs/MCP工具问题.md`）非本阶段产物，未处理、未提交。
5. 旧测试基线 warning（backend/app/config.py PydanticDeprecatedSince20）属冻结旧代码，按 P0 红线不修。

## 八、范围外确认（P1 明确不做）

Tier3 浏览器交互层（P3）、降级链/HITL 重构（P2）、LangGraph checkpointer 与多轮会话（P2）、前端迁移（P2）、A2A（P5）、修改旧代码（冻结延续到 P2）——本阶段均未触碰。
