# V2.0 P2 交付报告（P2 Task 10 验收）

- 分支：`v2.0`（P2 提交范围 `bbf1af2..70ed5b4`，共 15 commits；P1 终点 `92ed2f1`）
- 验收日期：2026-09-02（本地 22:30，UTC 14:30）
- 验收结论：✅ **全量测试 171 passed + ruff/mypy 全绿；eval mock 3/3 PASS（主验收证据）；真实模式 2/3 PASS（p0-search 因 ddgs 结果漂移 FAIL，根因定位，非代码回归，见偏差 D1）；E2E 冒烟全链路绿；前端 build 绿；旧代码冻结 0 改动**

## 一、全量测试与工程化门禁（Step 1）

```bash
uv run pytest tests/ -q          → 171 passed, 1 warning in 15.80s
uv run ruff check src/ eval/ tests/ → All checks passed!
uv run mypy src/browsepilot/     → Success: no issues found in 26 source files
```

- 基线：P1 验收 137 → P2 验收 171（+34）
- warning：`backend/app/config.py:19` PydanticDeprecatedSince20（旧代码冻结区，与 P1 相同，不修）

## 二、评测回归（Step 2）

### 2.1 mock 模式（确定性，主验收证据）

```bash
uv run python -m eval.run_eval --mock
```

| 场景 | 结果 | detail |
|---|---|---|
| p0-read-page | ✅ PASS | `keywords hit=['Python']` |
| p0-search | ✅ PASS | `keywords hit=['2024']` |
| p0-weather | ✅ PASS | `city='北京' expected='北京'; temperature_c=26.0 (number)` |

汇总：`[eval] 3/3 passed`。

### 2.2 真实模式（DeepSeek + Open-Meteo/ddgs 真实网络，尝试记录）

命令：shell 内导出 `BP_MODEL_API_KEY`/`BP_MODEL_BASE_URL`（值取自 `.env` 的 `OPENAI_API_KEY`/`OPENAI_BASE_URL`，仅进程内，未写文件、未提交；`base=https://api.deepseek.com/v1`）后 `uv run python -m eval.run_eval`。

| 场景 | 结果 | detail |
|---|---|---|
| p0-weather | ✅ PASS | `city='北京' expected='北京'; temperature_c=24.9 (number)`（终端 city 乱码为 Windows GBK 控制台显示问题，判定为 Python 字符串相等，非数据问题） |
| p0-search | ❌ FAIL | `keywords hit=[]`（两次运行结果一致，非 flaky） |
| p0-read-page | ✅ PASS | `keywords hit=['Python']` |

汇总：`[eval] 2/3 passed`。trace 落盘：`data/traces/ee5f4fec-1d86-4cc7-b093-d16b05b9cc97.jsonl`（gitignored，不提交）。

**p0-search FAIL 根因（已定位，非代码回归）**：trace 显示 `classify→decision(intent=search, confidence=1.0)→tool_call search status=success→answer` 全链路正常；但 ddgs 本次返回的 5 条结果全部无关（Python 3.8/3.10 相关页面，无 Python 3.13 发布时间信息），模型如实回答「根据提供的搜索结果，没有任何关于 Python 3.13 正式发布日期的信息……因此无法回答该问题」，最终答案不含关键字 "2024"。

- 这是 P1 报告遗留 #1 预警过的外部依赖漂移（P1 验收同场景 PASS，当时的 ddgs 结果含 "2024"）。
- search intent 的策略链只有 `["search"]`（`src/browsepilot/agent/graph.py::build_graph` 内 `_STRATEGY_CHAIN`，注释明确「search/read_page 无有意义回退，失败即明确降级」）；工具返回 success（有结果但无关）即终止链，降级链不做相关性判定——模型「诚实不捏造」是 v2 设计行为。
- 归入偏差 D1，建议 P4 评测集扩充时加固（备用搜索源/结果缓存，或检查项与「诚实不捏造」设计对齐）。
- 凭据注：T8 时期 `.env` 的 DeepSeek key 失效问题本环境已恢复（本轮真实调用成功），真实模式不再是 auth 阻塞。

## 三、E2E 冒烟（Step 3，真实服务 :8001）

启动：`uv run uvicorn browsepilot.app.main:app --port 8001`（同 env 导出，PID 28864；验收后 `taskkill /PID 28864 /T /F`，端口已释放）。以下全部为真实模型调用。

1. `GET /health` → `{"status":"ok","version":"2.0.0","model":"deepseek/deepseek-chat"}`（200 OK，服务日志确认）
2. `POST /chat/stream` 匿名新聊天（`{"task":"北京今天天气怎么样？","session_id":""}`，`--data-binary @file` 规避 Windows GBK 问题），SSE 三事件按契约顺序：

```
data: {"type": "phase", "phase": "start", "task": "北京今天天气怎么样？", "session_id": "dfcedae2-3b60-4765-97cd-77aff562aea3"}
data: {"type": "answer", "text": "北京今天晴，气温24.7℃，湿度58%，风速6.6公里/小时。"}
data: {"type": "done", "stop_reason": "completed", "result_data": {"city": "北京", "temperature_c": 24.7, ...}}
```

3. `GET /sessions` → **自动建行验证（T8 修复 f511000）**：匿名首聊自动落 1 行，`id == thread_id == dfcedae2-...`
4. `GET /history/{sid}` → `turns` 含本轮 `{task, answer, created_at}`（1 turn）
5. 第二轮同 session_id（「我刚才问了哪个城市的天气？」）→ 模型回答「根据数据，你刚才问的是**北京**的天气」→ **history 注入验证**；`GET /history/{sid}` → 2 turns
6. `POST /chat/resume`（已完成会话、无 pending）→ SSE 错误契约（HTTP 200 in-band error）：

```
data: {"type": "error", "message": "恢复任务失败：session 'dfcedae2-...' has no pending human action to resume"}
data: {"type": "done", "stop_reason": "error", "result_data": {}}
```

7. OpenAPI 完整性：`GET /openapi.json` 8 路径全列出（`/health`、`/sessions` GET、`/sessions/{id}` DELETE、`rename`/`pin` PATCH、`/history/{id}`、`/chat/stream`、`/chat/resume`）
8. 限流 sanity：未做 60 连发实时压测（brief 标注「可选，if quick」，实际不 quick）；配置为 `rate_limit_requests=60` / `rate_limit_window_seconds=60` / `session_budget_tokens=200000`（`src/browsepilot/app/config.py`）；T8 已有实机证据（60 次后 `429 {"detail":"rate limit exceeded"}` + `Retry-After: 60`；51 轮 seed 会话 → 429 budget），测试固化为 `tests/test_app/test_rate_limit.py`
9. 前端 `npm run build`（vue-tsc + vite）→ ✓ built in 3.59s（仅既有 chunk-size >500kB 警告，与 T8 相同）；dev-proxy 手测跳过（T8 已 Playwright 双阶段验证 12/12 断言 + 修复轮回归，本环境复用成本高，见偏差 D6）

服务日志 200s：`GET /health`、`POST /chat/stream` ×2、`GET /sessions`、`GET /history` ×2、`POST /chat/resume`、`GET /openapi.json`（中间一次 400 为 inline JSON 中文 GBK 编码问题的复现，改用文件后 200，非服务缺陷）。

## 四、旧代码冻结（Step 4）

```bash
git diff 92ed2f1..HEAD --stat -- browsepilot/backend browsepilot/browser_mcp
```

→ 空输出（0 文件）。`frontend-vue/` 改动仅限 T8/T8-fix 范围（8 files, +172/−392，T8 起解冻）。本任务（Task 10）零代码改动：仅新增本报告一个文件。P2 全部改动限于 `browsepilot/src/`、`browsepilot/eval/`、`browsepilot/tests/`、`browsepilot/docs/`、`browsepilot/frontend-vue/`。

## 五、P2 交付检查单

- [x] checkpointer 集成（SqliteSaver），多轮状态持久化 —— 运行期产物 `data/checkpoints.sqlite`（lifespan 接线 `src/browsepilot/app/main.py`）；测试覆盖
- [x] `/sessions` CRUD + `/history` + `/chat/resume` API（OpenAPI 8 路径完整，见冒烟 #7）
- [x] 降级链：weather→search 策略回退 + degradation_log 全程可追溯（trace 每尝试一条 `tool_call`；测试 `test_tool_failure_degrades_with_explicit_reason`、`test_search_intent_single_strategy_failure_is_final`）
- [x] HITL interrupt/resume 骨架可跑（测试固化 `test_hitl_interrupt_then_resume`；真实服务 resume 错误契约冒烟 #6）
- [x] llm_call trace 事件落地（TracingModelProvider；真实 eval trace 含 `llm_call structured/chat` 事件，见 2.2 trace）
- [x] 限流（429）+ 会话 token 预算（`tests/test_app/test_rate_limit.py` + T8 实机证据）
- [x] 前端迁移到 v2 API（chat/sessions/resume 全通；T8/T8-fix；本次 `npm run build` 绿）
- [x] 文章 5 素材（T9 `800566e` + review fix `70ed5b4`）
- [x] 全量测试 + eval 回归 + 冒烟全绿：pytest/ruff/mypy 全绿；eval mock 3/3、真实 2/3（偏差 D1，外部依赖漂移）；冒烟全链路绿
- [x] `backend/`、`browser_mcp/` 仍零改动（冻结到 P5）

## 六、P2 相对设计的偏差与决议（SDD ledger 摘要；controller 附完整清单）

| # | 任务 | 偏差/决议 | 提交 |
|---|---|---|---|
| D1 | T10 | p0-search 真实模式 FAIL：ddgs 结果内容漂移（无 "2024" 关键字来源），模型诚实不捏造 → 检查项落空。非代码回归；mock 3/3 为主证据；建议 P4 加固评测（备用搜索源/结果缓存） | —（记录） |
| D2 | T1 | msgpack 前向兼容注记：strict-msgpack 未来版本将拒绝未注册类型；`allowed_msgpack_modules=["browsepilot.agent.state"]` 注册点 `src/browsepilot/app/main.py::lifespan` 尚未落地（部署接线时补，文章 5 素材已注记） | 1a16f84 |
| D3 | T4 | `__interrupt__` API 现实：langgraph 1.1.9 `ainvoke` 不抛 `GraphInterrupt`，返回带 `__interrupt__` 键的暂停状态；brief 的 `pytest.raises` 描述作废，测试按现实断言（源级验证） | 0d7b19c |
| D4 | T5 | HITL session_id 透传修复：匿名会话 HITL 死路（server uuid4 不返回客户端）→ `phase/start` + `human_pending` 携带 effective session_id，端到端 resume 测试证明 | cd98420 |
| D5 | T7 | 测试自递归 bug：monkeypatch `time.monotonic` 后 lambda 再调用 → RecursionError，不可满足；改为确定性时钟闭包，语义一致 | 89db661 |
| D6 | T8 | 后端自动建会话行（controller 决议）：v2 后端原无任何路径调 `create_session` → `chat_stream` 遇不存在 session_id 自动建行；前端配套 `session_id||''`（422 修复）+ resume 失败状态恢复 | f511000 / 93b9fda |

## 七、遗留问题与观察

1. **ddgs 结果漂移（D1）**：P1 遗留 #1 的现实化。搜索工具「成功但无关」无兜底策略（by design）；建议 P4 评测加固 + 观察备用搜索源/结果缓存。
2. **凭据接线**：V2 `Settings` 只读 `BP_*` 前缀，`.env` 仍只有 v1 变量；真实模式评测/冒烟需 shell 导出 `BP_MODEL_API_KEY`/`BP_MODEL_BASE_URL`（不写文件）。后续可考虑 `.env` 增加 `BP_*` 段（不入库）。
3. resume 的 HTTP 层失败（429）前端已可恢复（93b9fda）；SSE 层 error+done 场景前端走 error 气泡收尾（waiting_human 横幅误导属 T8 deferred）。
4. 多 worker 部署下 sqlite 写并发 `SQLITE_BUSY`（T5/T8 deferred，部署文档注明）；check-then-insert 非原子（T8 deferred）；`/chat/resume` 不受预算门、成功后不 record_turn（T4/T7 deferred，P4 跟）。
5. 前端无 vitest 基建（T8 deferred），回归靠 curl + Playwright 断言。
6. trace 文件位置：每次运行落 `browsepilot/data/traces/<trace_id>.jsonl`（gitignored）。本次验收真实模式 trace：`ee5f4fec-1d86-4cc7-b093-d16b05b9cc97.jsonl`；mock trace 同目录。
7. 仓库既有未跟踪文件（`.pi/`、`AI_Agent_Demo.zip`、`docs/MCP工具问题.md`）非本阶段产物，未处理、未提交。
8. 旧测试基线 warning（`backend/app/config.py` PydanticDeprecatedSince20）属冻结旧代码，按 P0 红线不修。

## 八、范围外确认（P2 明确不做）

Tier3 浏览器交互层（P3）；评测集扩充与量化报告（P4）；A2A、旧代码删除、发布（P5）——本阶段均未触碰。
