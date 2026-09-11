# BrowsePilot V2.0 部署与运行指南

> **本文档由 P5 Task 4 编写（2026-09-11）**。命令、变量名、默认值**逐字取自当前代码与编排文件**
> （`src/browsepilot/app/config.py`、`start.sh`/`start.bat`、`Dockerfile`、`docker-compose.yml`、
> `eval/gate.sh`、根 `.github/workflows/ci.yml`、根 `.env.example`）；未验证的内容显式标注。
> 相关文档：架构 `docs/v2-architecture.md` · 评测 `docs/v2-eval.md` · 性能 `docs/v2-performance.md`。
> 本文档**不含任何密钥**——只出现变量名。

## 1. 环境前提

| 项 | 要求 | 说明 |
|----|------|------|
| Python | `requires-python = ">=3.11"`（`pyproject.toml`） | 开发/评测实测环境 Python **3.14.3**；Docker 镜像用 **3.13**（`Dockerfile`） |
| 包管理 | [uv](https://docs.astral.sh/uv/)（唯一入口） | `uv run` 跑一切 Python 命令；`gate.sh` 缺 uv 时 exit 127 |
| Node/npm | 仅前端演示层需要 | 版本未在仓库中固定（`frontend-vue/package.json` 无 `engines`）；`npm install` 后 `npm run dev` |
| Docker（可选） | 单实例部署用 | 本机实测 `Docker version 29.5.2` / `Docker Compose version v5.1.3`；本任务**未构建镜像** |
| 浏览器（Tier3 真实模式） | Playwright 1.59.0（`uv.lock`） | 本机缓存只有 chromium rev **1208**，而 1.59 默认要 **1217** → 默认启动报 "Executable doesn't exist"；用 `BP_BROWSER_CHANNEL=msedge` 或 `uv run playwright install chromium`（§6.2） |
| 控制台编码（Windows） | `PYTHONIOENCODING=utf-8` | 中文日志/评测输出会乱码；`gate.sh` 内部已设默认值 |

依赖安装（在 `browsepilot/` 下，`--all-extras` 才会装 pytest/ruff/mypy 等开发依赖）：

```bash
uv sync --all-extras          # 本地开发（含 dev extras）
uv sync --all-extras --frozen # CI 口径（不更新 uv.lock）
```

## 2. 环境变量全表（`BP_*` 前缀）

`Settings`（`src/browsepilot/app/config.py`）配置为 `env_prefix="BP_"` + `env_file=".env"` + `extra="ignore"`。
**只有带 `BP_` 前缀的变量会被读取**；`.env` 相对**启动时的 CWD** 解析（所以从 `browsepilot/` 启动）。
下表 18 个字段逐一取自该文件（默认值等于代码内声明值）：

| 环境变量 | 类型 / 默认值 | 说明与接线点 |
|----------|---------------|--------------|
| `BP_DEFAULT_MODEL` | str / `deepseek/deepseek-chat` | LiteLLM 模型 id（`_default_app` → `LiteLLMProvider`）；`/health` 也返回它。**真实模式必钉此值**（§5） |
| `BP_MODEL_API_KEY` | str / `""` | 传给 LiteLLM 的 `api_key`；空串 → `None`，回退 LiteLLM 的 provider 环境变量。仅进程内，不入库/不入 git |
| `BP_MODEL_BASE_URL` | str / `""` | 传给 LiteLLM 的 `base_url`（OpenAI 兼容网关）；空串 → `None` |
| `BP_TRACE_DIR` | str / `data/traces` | `JsonlTracer` 输出目录（一 trace 一 JSONL 文件；进程级一个 trace_id，见架构 §4.5） |
| `BP_SESSION_DB_PATH` | str / `data/sessions.sqlite` | `SessionStore` 的 SQLite 路径（会话元数据 + turns） |
| `BP_CHECKPOINT_DB_PATH` | str / `data/checkpoints.sqlite` | LangGraph `AsyncSqliteSaver` 路径（HITL interrupt/resume 必需）；**设为空串 = 不建 checkpointer**（无 resume；bench/部分测试用） |
| `BP_NAVIGATION_POLICY` | str / `allow_https` | 导航策略模式：`allow_https`（只放行 http/https）\| `allowlist`（再加域名白名单） |
| `BP_ALLOWED_DOMAINS` | list[str] / `[]` | 仅 `allowlist` 模式生效；**必须写 JSON 数组**（见下注）；空名单 = 全拒 |
| `BP_SESSION_TIMEOUT_SECONDS` | int / `300` | **未接线**：仅在 `config.py` 声明，当前图/请求路径不读（架构 §6） |
| `BP_MAX_STEPS` | int / `30` | **未接线**：同上（v1 遗留语义） |
| `BP_MAX_REPLANS` | int / `2` | **未接线**：同上 |
| `BP_CONSECUTIVE_FAILURES_THRESHOLD` | int / `3` | **未接线**：同上（v1 熔断参数） |
| `BP_RATE_LIMIT_REQUESTS` | int / `60` | 滑动窗口内允许的请求数（只拦 `/chat/stream`、`/chat/resume`，按客户端 IP） |
| `BP_RATE_LIMIT_WINDOW_SECONDS` | int / `60` | 窗口秒数；超限 429 的 `Retry-After` 值 |
| `BP_SESSION_BUDGET_TOKENS` | int / `200000` | 会话预算闸门：已记录 turns × 4000（估算）超过它 → 启动前 429 |
| `BP_BROWSER_HEADLESS` | bool / `true` | BrowserPool 是否 headless；HITL 人工介入需 `false`（可见窗口） |
| `BP_BROWSER_MAX_PAGES` | int / `3` | 池内 page 上限（新建/回收；必须 ≥1） |
| `BP_BROWSER_CHANNEL` | str / `""` | 空 = Playwright 自带 Chromium；`msedge`/`chrome` = 系统浏览器（接线处空串收敛为 `None`，`channel=""` 不是合法值） |

> **`BP_ALLOWED_DOMAINS` 写法（已验证）**：pydantic-settings 对 `list[str]` 字段按 JSON 解析 ——
> `BP_ALLOWED_DOMAINS='["github.com","baidu.com"]'` 可以；`BP_ALLOWED_DOMAINS=github.com,baidu.com`
> 会抛 `SettingsError`（本机实测）。该变量只在 `BP_NAVIGATION_POLICY=allowlist` 时影响行为。
>
> **v1 变量不生效（`BP_` 前缀陷阱）**：仓库里的 `.env.example` 与 `.env` 是 **v1 命名（无前缀）** ——
> `OPENAI_API_KEY` / `BROWSER_CHANNEL` / `ALLOWED_DOMAINS` 等**不会**被 v2 的 `Settings` 读取
> （`extra="ignore"` 静默忽略）；`tests/test_app/test_main.py::test_settings_browser_fields_follow_bp_env_prefix`
> 专门钉住这一点。后果：app 能正常启动（`/health` 200），但模型 key 实际为空 → 真实任务调用失败。
> 真实模式必须显式导出 `BP_MODEL_API_KEY` / `BP_MODEL_BASE_URL`（§5）。
>
> **不加前缀的例外**：LiteLLM 自身的 provider 环境变量（如 `OPENAI_API_KEY`）只在 `BP_MODEL_API_KEY`
> 为空时由 LiteLLM 内部回退读取（v2-eval §6.3 同口径）——本机未验证具体回退链，推荐显式传 `BP_*`。

## 3. 本地运行（Windows / Linux）

### 3.1 一键脚本

| 脚本 | 平台 | 行为（逐字对应文件内容） |
|------|------|--------------------------|
| `bash start.sh` | Linux/WSL/Git Bash | `uv run uvicorn browsepilot.app.main:app --host 127.0.0.1 --port 8001` 后台起；`(cd frontend-vue && npm run dev)` 后台起；`trap cleanup` 在 Ctrl+C/退出时 kill 两个进程；打印 `http://localhost:8001` / `http://localhost:5173` |
| `start.bat` | Windows | 检查 `.venv\Scripts\python.exe` 与 `frontend-vue\node_modules`（缺则报错退出）；**清理端口**：`netstat -ano \| findstr ":8001 :5173" \| findstr "LISTENING"` 取 PID → `taskkill /pid <pid> /f`；分别 `start` 两个新窗口：`.venv\Scripts\python.exe -m uvicorn browsepilot.app.main:app --host 127.0.0.1 --port 8001` 与 `cd frontend-vue && npm run dev` |

### 3.2 手动启动（推荐用于排障）

```bash
# 后端（：8001）—— 在 browsepilot/ 下
uv run uvicorn browsepilot.app.main:app --host 127.0.0.1 --port 8001

# 前端（：5173）—— 另开一个终端
cd frontend-vue && npm install && npm run dev
```

- 健康检查：`curl http://127.0.0.1:8001/health` → `{"status":"ok","version":"2.0.0","model":"<BP_DEFAULT_MODEL>"}`。
- OpenAPI 文档：`http://127.0.0.1:8001/docs`（spec §3.3：API 文档是产品契约）。
- 前端 dev server 把 `/api` 代理到 `http://127.0.0.1:8001`（`frontend-vue/vite.config.ts` 的 `rewrite: /^\/api/ → ""`）；
  app **没有** CORS 中间件，也不挂载静态前端 → 演示必须走 Vite dev server（本机未验证生产构建托管方案）。
- Tier3 启动失败不影响启动：`BrowserPool.start()` 抛异常时记日志、把 executor 的 browser 置 `None`，
  6 个浏览器工具统一返回 `browser_unavailable`，app 照常服务（诚实降级）。

### 3.3 数据与目录

运行时写入（全部在 `.gitignore` 的 `data/` 下，不入 git）：`BP_TRACE_DIR`（trace JSONL）·
`BP_SESSION_DB_PATH`（会话库）· `BP_CHECKPOINT_DB_PATH`（HITL checkpoint）· `data/eval-reports/`（评测报告）·
`data/bench/`（性能原始样本）。首次启动会自动建目录/表。

## 4. Docker（单实例）

### 4.1 Dockerfile（当前内容摘要）

两阶段构建（`ghcr.io/astral-sh/uv:python3.13-bookworm-slim` → `python:3.13-slim-bookworm`）：

1. builder：`COPY pyproject.toml uv.lock` → `uv sync --all-extras --frozen --no-dev --no-install-project` →
   `COPY src ./src`、`COPY eval ./eval`、`COPY frontend-vue ./frontend-vue` → `uv sync --all-extras --frozen --no-dev`；
2. runtime：从 builder 拷 `.venv` + `src` + `eval` + `frontend-vue`；`ENV PATH="/app/.venv/bin:$PATH"`；
   `EXPOSE 8001`；`CMD ["uvicorn", "browsepilot.app.main:app", "--host", "0.0.0.0", "--port", "8001"]`。

> 注：**本任务未执行 `docker build`**（不做真实运行验证）；以上为文件内容通读。镜像里 `frontend-vue/`
> 只是被 COPY 进来，app 没有静态挂载点（grep 无 `StaticFiles`/`mount`）——是否需要取决于私有化方案。
> `.dockerignore` 排除：`.venv/`、`__pycache__/`、`*.pyc`、`.pytest_cache/`、`data/`、
> `frontend-vue/{node_modules,dist,.vite}/`。

### 4.2 docker-compose.yml（当前文件内容，逐字）

```yaml
services:
  app:
    build: .
    ports:
      - "8001:8001"
    env_file:
      - .env
    volumes:
      - ./data:/app/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
```

```bash
cd browsepilot
docker compose up --build -d      # 构建并后台启动
docker compose ps                 # healthcheck 状态
docker compose logs -f app
docker compose down
```

- `.env` 从宿主机读取并以**环境变量**形式注入容器（文件本身不挂载）；`./data` 挂载持久化运行数据。
- **`.env` 不存在时 compose 直接报错（本机实测，Compose v5.1.3）**：
  `env file …\.env not found: … The system cannot find the file specified.` —— `.env` 在 `.gitignore` 里，
  首次部署需自备（`docker compose config` 在 `.env` 存在时解析通过）。
- 容器里必须是 **`BP_*`** 变量才生效（§2）；`.env` 只有 v1 命名时容器内等于没有模型 key。

## 5. 真实模式前提（Tier3 / 真实模型 / HITL）

与 `docs/v2-eval.md` §1/§6.3 同源（逐字口径）：

| 前提 | 值 | 为什么 |
|------|----|--------|
| 模型 | `BP_DEFAULT_MODEL=deepseek/deepseek-chat` | 思考模型（deepseek-flash / v4-pro）拒绝 forced `tool_choice` → `structured` 调用报错（`StructuredOutputError`） |
| 模型 key | `BP_MODEL_API_KEY`（+ 可选 `BP_MODEL_BASE_URL`） | 仅进程内环境变量；留空时回退 LiteLLM 的 provider 环境变量 |
| 浏览器（tier3） | `BP_BROWSER_CHANNEL=msedge`，或 `uv run playwright install chromium` | 本机 Playwright 1.59 需要 chromium rev 1217、缓存只有 1208 → 默认启动失败；池起不来时评测报告环境行写 `unavailable (start failed)`，tier3 的失败**在报告里**归因环境而非任务 |
| HITL 人工介入 | `BP_BROWSER_HEADLESS=false` | 需要可见浏览器窗口；且 `hitl_login_detect` 只在显式 `--tag hitl-live` 时被选中（需可达登录墙站点） |

示例（不落盘、不写进任何文件）：

```bash
cd browsepilot
export BP_DEFAULT_MODEL=deepseek/deepseek-chat
export BP_MODEL_API_KEY=...        # 值自备；勿提交
export BP_MODEL_BASE_URL=...       # 可选
export BP_BROWSER_CHANNEL=msedge
uv run uvicorn browsepilot.app.main:app --host 127.0.0.1 --port 8001
```

## 6. 评测与门禁

### 6.1 本地门禁（与 CI 同口径）

```bash
cd browsepilot && bash eval/gate.sh        # 或仓库根：bash browsepilot/eval/gate.sh
```

`set -euo pipefail`，四步依次（任一步失败即停 + 非零退出；全绿打印 `[gate] ALL PASS`）：
`uv run pytest tests/ -q` → `uv run ruff check src/ eval/ tests/` → `uv run mypy src/browsepilot/` →
`uv run python -m eval.run_eval --mock`。脚本开头打印环境行（cwd/分支/HEAD/uv 版本/时间）。
**门禁只跑 mock**：无网络、无 key、无浏览器。当前读数：`810 passed`（2026-09-11，P5-T3 后）、
eval mock `passed=8 skipped=4 disabled=4`（exit 0）；口径与当前基线见 `docs/v2-eval.md` §2/§6.1。

### 6.2 eval 与性能脚本

```bash
# 离线（CI 口径）
uv run python -m eval.run_eval --mock

# 真实模式 acceptance（需 §5 的前提）
BP_DEFAULT_MODEL=deepseek/deepseek-chat BP_MODEL_API_KEY=... BP_BROWSER_CHANNEL=msedge \
  uv run python -m eval.run_eval --tag acceptance --repeat 3 --md data/eval-reports/

# 性能基线（可离线：--skip-real）
uv run python -m eval.bench --repeat 5 --real-repeat 3 --json data/bench/p5-t3.json
```

参数逐字对应 `eval/run_eval.py`：`--mock` · `--tag`（可重复；缺省 = 全部可运行场景）· `--json PATH` ·
`--md PATH`（目录 = 写 `<目录>/<时间戳>.json|md`）· `--repeat N`（≥1）。exit code = 任一非 fragile 失败 → 1。
tag 语义（`smoke` / `acceptance` / `browser` / `hitl` / `hitl-live` / `degrade`）与 fragile 计法见
`docs/v2-eval.md` §3.3/§4.3。

### 6.3 CI 现状：workflow 已就绪，但**从未在 GitHub Actions 上实际执行过**

- 文件：仓库根 `.github/workflows/ci.yml`（触发：push `master`/`v2.0` + PR；步骤 Install → Lint → Types →
  Tests → Eval (mock)，全部 `working-directory: browsepilot`；Install = `uv sync --all-extras --frozen`）。
- 事实：本地分支**不推到远程**（用户明确不推送）→ **没有任何一次 Actions 运行记录**；CI 口径目前只有"声明"，
  没有"执行证据"。发布报告引用 CI 时必须写"workflow 已就绪 + 本地 `gate.sh` 等价验证（exit 0）"，
  **不得**写"CI 通过 / CI 绿"。
- 补口：首次 `git push` 后先核对一次真实 Actions 结果（run id / 时间 / 结论），再更新本节。
- 来源与完整口径：`docs/v2-eval.md` §6.5（P5-T0b）。

## 7. 常见问题（FAQ）

1. **端口被占用（8001 / 5173）**
   - Windows：`start.bat` 已内置清理 —— `netstat -ano | findstr ":8001 :5173" | findstr "LISTENING"` 取 PID，
     `taskkill /pid <pid> /f`；手动排查用同样的 netstat 过滤。
   - Linux/macOS：`start.sh` **不做**端口清理；可用 `lsof -i :8001` / `fuser -k 8001/tcp`
     （命令未在本机验证——开发机为 Windows）。
2. **Playwright 启动失败 / chromium rev 不匹配**
   - 症状：`Executable doesn't exist …chromium-1217…`（本机缓存只有 `chromium-1208`，`~/AppData/Local/ms-playwright`）。
   - 处置 A（推荐，免下载）：`BP_BROWSER_CHANNEL=msedge`（用系统 Edge）；
   - 处置 B：`uv run playwright install chromium`。
   - 池起不来的完整后果与报告口径见 `docs/v2-eval.md` §6.3/§8.6。
3. **`.env` 改了变量但行为没变**：确认变量名带 `BP_` 前缀（§2）；v1 命名的变量被 `extra="ignore"` 静默忽略。
   最可靠的做法是在 shell 里 `export BP_...` 后启动（T7 的实操口径）。
4. **`BP_ALLOWED_DOMAINS` 报 `SettingsError`**：它是 `list[str]`，必须写 JSON 数组（§2 注），不是逗号分隔。
5. **`docker compose up` 直接报 `env file …\.env not found`**：`.env` 不在 git 里，首次部署要自备（§4.2）。
6. **HITL 场景总是 FAIL**：需可达的登录墙站点 + `BP_BROWSER_HEADLESS=false`，且 `hitl_login_detect` 只在
   `--tag hitl-live` 下被选中；`needs_human` 链路在本机**未端到端验证**（原因与证据：`docs/v2-eval.md` §8.2）。
7. **成本/`cost_usd` 一直是 `null`**：LiteLLM 远端价格表超时（未采集，不估算）——`docs/v2-eval.md` §8.1。
8. **trace 文件只有一个/越来越大**：当前实现是进程级单 tracer（一个 trace_id、一个 JSONL 文件），
   见 `docs/v2-architecture.md` §4.5/§6；重启进程才会换文件。

---

*文档结束。部署相关的口径变化请同步 `docs/v2-eval.md`（环境/门禁）与 `docs/v2-architecture.md`（机制）。*
