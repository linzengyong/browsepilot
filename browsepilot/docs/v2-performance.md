# V2.0 性能基线（P5 Task 3）

> 一次性真实读数（**2026-09-11**，本机，`uv run python -m eval.bench`），**逐字取自**产物
> `data/bench/p5-t3.json`（环境 + 每项全部原始样本）与 `data/bench/p5-t3-run.log`（stdout 原文）
> —— 未采集的项写"未采集"，**不估算**。
> 本页只量**数字**、不做 PASS/FAIL（断言在评测场景里），也**不是门禁**：`eval/gate.sh` 不含 bench。
> 口径：延迟 = **墙钟毫秒**；P50/P95 = `eval/report.percentile`（线性插值）—— 与 T7 报告同一把尺子，
> 所以本页数字与 `docs/v2-eval.md` §7 可以直接对着读（§4 说明**哪些不能混用**）。

## 1. 环境与测量方法

| 项 | 值 |
|----|----|
| 日期 / 时区 | 2026-09-11T21:21:35+08:00 |
| 代码版本 | `3d51612`（分支 `v2.0`，`git_dirty=true` —— 工作区含未提交改动，见 §5） |
| 模型 | `deepseek/deepseek-chat`（真实段唯一模型；`BP_DEFAULT_MODEL`） |
| 浏览器 | `channel=msedge`，`headless=true`，`max_pages=3`（`BP_BROWSER_CHANNEL=msedge`） |
| 机器 | `Windows-11-10.0.26200-SP0` · AMD64 / Intel64 Family 6 Model 151 / **12 cpus** |
| Python | **3.14.3**（`browsepilot\.venv\Scripts\python.exe`）· uv 0.10.4 |
| 重复次数 | mock / 冷启动 / health：**n=5**；real 端到端：**n=3** |
| 凭据 | `BP_MODEL_API_KEY ← OPENAI_API_KEY`（进程内映射，T7 §7.1 口径；未落盘） |

复现命令（在 `browsepilot/` 下，整轮实测 ≈21s，`EXIT=0`）：

```bash
set -a && source .env && set +a
export BP_DEFAULT_MODEL=deepseek/deepseek-chat BP_BROWSER_CHANNEL=msedge
uv run python -m eval.bench --repeat 5 --real-repeat 3 --json data/bench/p5-t3.json
```

`eval/bench.py` 的分节与方法（无 pytest 依赖，直接 `python -m`）：

| 分节 | 方法 | 读什么 |
|------|------|--------|
| 1. 冷启动 | `sys.executable -c <code>` **子进程**（每次新解释器，cwd=`browsepilot/`）：`pass` / `import browsepilot.app.config` / `import browsepilot.app.main` | 解释器基线、依赖链 import、完整冷启动 |
| 2. `create_app` | 进程内装配（fake provider/executor、`checkpoint_db_path=""`）：不读 .env、不建 sqlite、不起池 | 稳态装配成本；首次调用单列 |
| 3. `/health` | TestClient（ASGI **进程内**，不含 socket/HTTP 栈）：首次请求单列，其余取 P50/P95 | 路由 + pydantic 响应编码成本 |
| 4. tier1 端到端（mock） | `eval.harness.run_scenario`（内部即 `run_task`）跑 `t1_weather_beijing`，FakeProvider/FakeExecutor | **噪声基线**（离线、零 token） |
| 5. tier1 端到端（real） | 同一条场景 + 真模型 + 真服务（天气走网络）；`harness.build_real_deps` + `pool_lifecycle`，每场景后 `executor.aclose()` | 端到端墙钟、token、浏览器池启动成本 |

> tier2 / tier3 的真实延迟**不重跑**：直接引用 T7 数据（§4 表）。
> 退出码：全部分节 `ok` / `skipped` → **0**；任一分节 `failed`（如 real 段整体跑不起来）→ **1**
> —— 读数不合格要让调用方一眼看见（但不做耗时阈值断言，见 §5）。

## 2. 结果

### 2.1 冷启动与装配（n=5）

| 指标（新解释器子进程） | P50 | P95 | min | max |
|------------------------|-----|-----|-----|-----|
| interpreter baseline（`python -c pass`） | **27.11 ms** | 27.97 | 26.88 | 28.08 |
| `import browsepilot.app.config`（浅层：pydantic-settings） | **174.61 ms** | 176.93 | 172.98 | 177.33 |
| `import browsepilot.app.main`（**冷启动**） | **699.36 ms** | 738.46 | 684.86 | 743.99 |

- **冷启动 − 解释器基线 = 672.25 ms**（P50 差额）：这条差额就是"import 成本 + 生产装配"。
  `import browsepilot.app.main` 在模块级执行 `app = _default_app()`，所以这 672 ms 里同时含
  Settings 读取、`SessionStore` / `BrowserPool` / `AppToolExecutor` 构造（池的 `start()` 不在其中
  —— 那是 lifespan 的事）。
- `create_app`（进程内、import 已热）：**P50 2.75 ms**（P95 3.09，min 2.66，max 3.16）；
  **首次调用 12.96 ms** —— 首装配含惰性初始化（pydantic/FastAPI 响应模型首次编译），4.7 倍于稳态。

### 2.2 `/health`（n=5，TestClient 进程内）

| 指标 | P50 | P95 | min | max |
|------|-----|-----|-----|-----|
| `GET /health` | **0.51 ms** | 0.74 | 0.44 | 0.79 |

- 首次请求 1.50 ms（ASGI/客户端惰性初始化，单列不计入 P50）；响应体
  `{"status": "ok", "version": "2.0.0", "model": "deepseek/deepseek-chat"}`。
- 口径偏差：**不含 socket/HTTP 栈**（TestClient 直连 ASGI）。本地真实 HTTP 往返的读数本页未采集。

### 2.3 端到端延迟：tier1 天气（mock，n=5）—— 噪声基线

| 指标 | P50 | P95 | min | max |
|------|-----|-----|-----|-----|
| `run_task` 墙钟（`t1_weather_beijing`） | **11.41 ms** | 13.67 | 11.30 | 14.15 |

样本：`14.15 / 11.75 / 11.34 / 11.30 / 11.41` ms；`stop_reason=completed`（5/5）、
工具 `weather`（5/5）；**无 token 数字**（FakeProvider 不报 usage，快照只有 `chat_calls`/`model_calls` 覆盖度键）。
读法：这是**本机管线本身**的开销（LangGraph 节点调度 + 工具调用 + state 传递），
real 读数减掉它剩下的才是模型/网络/服务成本。它同时说明：mock 跑得快**不代表**产品快。

### 2.4 端到端延迟：tier1 天气（real，n=3）

| 指标 | P50 | P95 | min | max |
|------|-----|-----|-----|-----|
| `run_task` 墙钟（`t1_weather_beijing`） | **2587.37 ms** | **8541.78 ms** | 2249.24 | 9203.38 |

- 样本：`9203.38 / 2587.37 / 2249.24` ms（第 1 次 9.2s 是模型/服务侧的长尾；n=3 分辨不出"首跑抖动
  vs 网络波动"—— 同一命令前一次运行的同一位置是 5.1s/2.3s/2.2s，见 §5）。
- 结果面：`stop_reason=completed` 3/3、工具 `weather` 3/3、答案含"北京"**3/3**、`errors=[]`。
- token：每次 `101 / 101 / 102`，**合计 304**；覆盖度 **3/6 模型调用有计量**（`chat_only` 口径 →
  下界：每个任务 1 次 chat 有 usage 面 + 1 次 structured 无 usage 面）。
- **浏览器池启动一次：776.01 ms**（`pool_started=true`，单样本）。池的生命周期跨场景/跨请求，
  所以这笔成本**摊薄**在之后的所有 tier3 任务上；tier1 天气链**不需要**浏览器，本次它也真的没被用到
  （日志 `created=0, reused=0`）。

## 3. 已知瓶颈（读上面的数字之前先读这一节）

1. **模型网络延迟主导**（唯一的主导项）。同一条 tier1 任务：mock P50 11.41 ms vs real P50 2587.37 ms
   —— **≈227 倍**，mock 只占 real 的 **0.44%**；即一次任务 ~99.6% 的墙钟花在"模型调用 + 真实服务"
   上（天气服务与 ddgs 都在网络上）。尾部更明显：real 的 P95 / max = 8541.78 / 9203.38 ms，是中位数的
   3.3–3.6 倍 —— 波动在外网，不在图里。（严格说 mock 与 real 只是同一张图的两套依赖（Fake vs 真），
   差额读作**量级归因**，不作精确分解。）
2. **冷启动含 import 成本**，且它是**固定**成本：672.25 / 699.36 ≈ **96%** 的冷启动时间不在解释器本身
   （基线 27.11 ms），而在依赖链 import（litellm/langgraph/playwright 系）与生产装配。
   `create_app` 稳态只要 2.75 ms → "app 组装"不是成本，"把依赖拉起来"才是。
   服务常驻时这笔成本只付一次（进程启动）；容器/函数式部署要按 ~0.7s 计启动预算。
3. **P95 由搜索 / 浏览器场景决定**，不是天气链。本页 tier1 天气的 P95 = 8541.78 ms 只是"模型+外网响应尾"；
   T7 的样本池 P95 = **33380.9 ms**、单条极值 **45503.1 ms**（R3 `t2_search_structural`，ddgs 搜索链路），
   tier1 整体 P95 = 43327.11 ms。搜索/浏览器的**多步 + 外网**形状把 P95 拉到 30s 级：读到 P95 时先问
   "这条场景走没走网络/浏览器"。
4. **浏览器池启动是一次性成本**（实测 776.01 ms）：跨场景共享 → 摊薄；但"冷容器里第一个 tier3 任务"
   要额外付这 ~0.78s（还没算首次 `browser_open` 的导航成本）。
5. **首次调用比稳态贵**（`create_app` 12.96 → 2.75 ms；`/health` 首次 1.50 → 0.51 ms）：
   惰性初始化/缓存预热；压测或容量估算用稳态值，启动预算用首值。
6. **mock 数字不可当产品数字**：FakeProvider/FakeExecutor 没有模型、没有网络、没有浏览器；
   11.41 ms 证明的是"管线可跑"，不是"产品快"。

## 4. 与 T7 数据（`docs/v2-eval.md` §7）的关系

**A. 独立复现：同一条场景、同一个模型，读数对得上。**

| 场景 | 来源 | 3 次墙钟（ms） | tokens / 次 |
|------|------|----------------|-------------|
| `t1_weather_beijing` | 本页 §2.4（2026-09-11 21:21，n=3） | 9203.38 / 2587.37 / 2249.24 | 101 / 101 / 102 |
| `t1_weather_beijing` | T7 §7.2（2026-09-11 19:49，3 轮） | 2826 / 2854 / 2465 | 101 / 99 / 102 |

P50 2587.37 ms 落在 T7 三次（2465–2854 ms）的区间内，token 差 ≤3%（101–102 vs 99–102）——
两组独立运行（相隔约 1.5 小时、不同的网络时刻）互相印证了"tier1 天气 ≈ 2.2–9.2s、~100 token/次"
这个量级；T7 的跨轮 P50 stdev = 328.77 ms 与本页 3 个样本的散布（2249–9203）同向：
**天气链的波动主要来自网络，不是产品逻辑**。

**B. tier2 / tier3 不重跑，直接引用 T7 的分层读数（样本池口径，见 §7.2）**：

| tier | 执行 | P50 (ms) | P95 (ms) | tokens |
|------|------|----------|----------|--------|
| tier1（18 次，含搜索场景） | 18 | 11609.65 | 43327.11 | 6914 |
| tier2（抓页，6 次） | 6 | 3213.58 | 4340.56 | 1720 |
| tier3（浏览器，6 次） | 6 | 2378.60 | 3090.64 | 1557 |
| **样本池（30 次）** | 30 | **3221.3** | **33380.9** | 10191（下限） |

**C. 不能混用的地方（引用纪律）**：

- T7 = **30 次场景运行**（10 场景 × 3 轮）的 acceptance 样本池，含搜索/浏览器/失败重试路径，
  token 口径是 `chat_only` 的**下限**（30/63 次调用有计量）；本页 = **单场景** 3 次 + mock 5 次 +
  冷启动/health 的新读数 —— 本页的 P95 只是天气链的尾，**不等于** V2 的 P95。
- T7 的 P95 里含"依赖延迟"（ddgs/天气服务），本页的 §2.3/§2.4 是同一个混合读数；
  两页都遵循同一条读法：**P50 读产品侧，P95 读依赖侧**。
- 本页新增了 T7 没有的面：冷启动（§2.1）、`/health`（§2.2）、`create_app`（§2.1）、
  浏览器池启动（§2.4）—— T7 报告只有场景级墙钟与 token。

## 5. 复现、限制与后续

复现：

- 只跑离线部分：`uv run python -m eval.bench --repeat 5 --skip-real`；无凭据时 real 段**自动** SKIP
  （打印原因），mock 段照跑；`--real-repeat 1` 可只跑一次真实。退出码语义见 §1。
- 原始证据：`data/bench/p5-t3.json`（环境 + 全部样本）、`data/bench/p5-t3-run.log`（stdout 原文）；
  `data/` 在 `.gitignore` 里（不入库，按需重跑生成）。

已知限制：

- **单机、单网络出口、样本量小**（real n=3）：读数随机器负载与出口质量漂移；换模型/换网络不迁移。
- **同一命令的重跑散布不小**：本页数字来自提交版脚本的一次运行；同日晚 21:19 的前一次运行（脚本仅在
  real 段失败路径/退出码上有差异，**测量口径相同**）读到 real P50 **2349.42** / P95 **4787.16**、
  mock P50 **13.24** —— P50 差 ~10%，**P95 差近一倍**。所以：单次 n=3 的**尾部不是稳定读数**，
  引用 P95 时必须写出样本量（本页与 T7 都给了 n）。
- **Windows 冷启动**含文件系统/Defender 抖动（本页 min 684.86 / max 743.99 ms）；Linux 上不应直接套用。
- `git_dirty=true`：这次测量的代码 HEAD 是 `3d51612`，但工作区还有未提交改动（本页与 `eval/bench.py`
  即其中一部分）—— 未来引用时请连同 commit 一起看。
- `/health` 是 TestClient 口径（无 socket）；本页**未采集**真实 HTTP（uvicorn + curl）与并发下的
  读数，也未采集成本（`cost_usd`）—— 未采集不估算。
- bench **不做阈值断言**：耗时是机器相关的读数，硬阈值会变成假红灯。若要进 CI，只能进 **mock 段**
  （离线可跑）；真实段需凭据 + 网络，不进 CI。
