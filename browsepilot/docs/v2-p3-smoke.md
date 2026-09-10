# P3 Tier3 真实浏览器冒烟验收记录（T6）

> 本文档只记录观测结果与可复现片段，**不改任何 `src/` 代码**。冒烟脚本是临时 harness，
> 放在 `%TEMP%\t6-smoke\`（未入库）；下面的片段足够重跑。
> **无密钥**：真模型 E2E 的凭据只在 shell 进程内导出（`BP_MODEL_API_KEY`/`BP_MODEL_BASE_URL`），
> 未写入任何文件、未提交；本文档不包含 key/token（只记录长度断言与 `.env.example` 里已有的 base URL）。

## 0. 环境

| 项 | 值 |
|----|----|
| 日期 | 2026-09-10 22:52–23:05（本地） |
| OS / 解释器 | Windows (win32) / Python 3.14.3（`uv run`，仓库 `.venv`） |
| Playwright | 1.59.0 |
| 浏览器通道 | **`channel="msedge"`**（Edge 134.0.3124.93，headless=True）——本机缓存只有 `chromium-1208`，PW 1.59 默认要 `chromium_headless_shell-1217`：**默认启动会失败**（见 §5.6） |
| 池参数 | 冒烟 `BrowserPool(headless=True, max_pages=5, channel="msedge")`（`max_pages=5` 避免冒烟过程中淘汰旧页；app 默认 `browser_max_pages=3`） |
| 策略 | `NavigationPolicy(mode="allow_https", allowed_domains=[])`（`Settings` 默认，`.env` 无 `BP_NAVIGATION_POLICY`） |
| 真模型 | `deepseek/deepseek-chat`（`Settings.default_model` 默认值）+ `base_url=https://api.deepseek.com/v1`；`BP_MODEL_API_KEY` 长度 35（值不记录） |
| 网络可达性 | ✅ example.com / www.bing.com（→ cn.bing.com）/ zhihu / bilibili / jd / taobao / csdn / weibo；❌ github.com、accounts.google.com（15s 超时） |

运行方式（Windows Git Bash；`PYTHONIOENCODING=utf-8` 必加：控制台默认 GBK，JSON 里出现 `©` 会让 harness 自己崩，与产品无关）：

```bash
cd D:/AI_Agent_Demo/browsepilot
PYTHONIOENCODING=utf-8 uv run python "$TEMP/t6-smoke/layer1_v3.py"   # Layer 1（约 46s）
PYTHONIOENCODING=utf-8 uv run python "$TEMP/t6-smoke/layer15_hitl.py" # Layer 1.5 HITL（约 4s）
export BP_MODEL_API_KEY="$(grep '^OPENAI_API_KEY=' .env | cut -d= -f2-)"
export BP_MODEL_BASE_URL="$(grep '^OPENAI_BASE_URL=' .env | cut -d= -f2-)"
PYTHONIOENCODING=utf-8 uv run python "$TEMP/t6-smoke/layer2_e2e.py"   # Layer 2 真模型 E2E（约 8s）
```

## 1. 结论总览

| # | 场景 | 结果 |
|---|------|------|
| S1 | 池启动 + example.com + 快照 refs | **PASS** |
| S2 | bing 搜索（snapshot→type→click 提交→URL 变化→结果 refs） | **PASS** |
| S3 | 策略门（私有 IP / 回环 / `file:` / `javascript:` 全拒 + 执行器路径拒且不建页） | **PASS** |
| S4 | example.com 点击链接 + 动作后复查放行 | **PASS**（一次假失败，见 §5.2） |
| S5 | 登录页检测（真实 zhihu `/signin` → `login/high` + executor `needs_human`） | **PASS** |
| S5b | 任务书候选 github/google 登录页 | **SKIP（网络超时）** |
| S6 | 失败导航后复用池页（异常复现） | **PASS（复现成功）**，异常见 §5.1 |
| S7 | bing 提交控件两路验证（click ref vs Enter） | **PASS** |
| L2 | 真模型 + 真 app `/chat/stream` 走 tier3 | **PASS** |
| L1.5 | API 层 HITL：登录墙 → `human_pending` | **PASS** |
| — | 验证码 / 确认弹窗真实页 | **SKIP**（未找到稳定可复现页面，见 §4） |
| — | 前端（Vue）层 HITL | **SKIP**（仅 API 层断言；前端非本任务范围） |

## 2. 逐场景记录（场景 | 命令/片段 | 观测 | 结论）

### S1 池启动 + example.com + 快照 —— PASS

片段：见 §3-a。命令：`PYTHONIOENCODING=utf-8 uv run python "$TEMP/t6-smoke/layer1_v3.py"`。

```
{"tag":"s1_pool","started":true,"channel":"msedge","headless":true,"max_pages":5,"seconds":0.8}
{"tag":"s1_navigate","status":"success","url":"https://example.com/"}
{"tag":"s1_snapshot","ref_count":5,"title":"Example Domain",
 "refs":[{"label":"e2","role":"generic"},{"label":"e3","role":"heading","name":"Example Domain"},
         {"label":"e6","role":"link","name":"Learn more"}]}
```

* 池 `start()` 0.8s（msedge，headless）；`navigate` 成功；`page_snapshot_refs` 产出 **5 条 ref**（`[ref=eN]` 只在 `aria_snapshot(mode="ai")` 下出现，与实现注释一致）。
* 注：example.com 现在的链接文案是 **"Learn more"**（不是任务书写的 "More information..."）。

### S2 bing 搜索（type + 提交 + 二次快照）—— PASS

片段：见 §3-a 的 `type_text` / `click` 部分。

```
{"tag":"s2_navigate","status":"success","url":"https://cn.bing.com/"}          # www → cn 重定向
{"tag":"s2_snapshot","ref_count":27,"boxes":[{"label":"e6","role":"searchbox","name":"输入搜索词"}],
 "button_refs":[]}
{"tag":"s2_type","target":"e6","resolution":{"kind":"ref","selector":"aria-ref=e6"},"attempts":1}
{"tag":"s2_value_check","value":"Python 3.13"}                                  # 幂等填值真的落进输入框
{"tag":"s2_submit","how":"click ref=e7 role=generic name=搜索网页","status":"success",
 "url":"https://cn.bing.com/search?q=Python+3.13&search=&form=QBLH"}
{"tag":"s2_after","url_changed":true,"ref_count":30,"title":"Python 3.13 - 搜索","body_has_python":true}
```

* 输入框在快照里是 `searchbox "输入搜索词" [ref=e6]`；`type_text` 用 ref 命中一级候选、1 次成功，`input_value()` 复核 = `Python 3.13`。
* **提交控件不是 `role=button`**：首页快照里 `button_refs: []`，提交按钮是 `generic "搜索网页" [ref=e7]`（内含 img）。点击 **e7 成功提交**（0.2s）；`keyboard Enter` 同样提交（见 S7）。两条路都验证了。
* URL 变化 ✓、新快照 **30 refs** ✓、标题 "Python 3.13 - 搜索"、正文首屏含 python.org 结果 ✓。

### S3 策略门 —— PASS

片段：见 §3-b。

```
NavigationPolicy().validate(...)（allow_https）：
  https://169.254.169.254/latest/meta-data/ → false  private_address_blocked
  http://127.0.0.1:8080/                    → false  private_address_blocked
  https://[::1]/                            → false  private_address_blocked
  file:///C:/Windows/win.ini                → false  protocol_blocked: file:
  javascript:alert(1)                       → false  protocol_blocked: javascript:
  https://example.com                       → true   (reason 为空)

executor 路径（AppToolExecutor.call("browser_open", ...)）：
  file:///C:/Windows/win.ini                → status=error, error_type=navigation_blocked,
                                              pages_created_delta=0
  https://169.254.169.254/latest/meta-data/ → status=error, error_type=navigation_blocked,
                                              pages_created_delta=0
```

* 每个拒绝都带稳定 reason（可直接当用户文案）；**拒绝路径不建页**（`pool.pages_created` 增量 0），D4 的"把关在动作之前"在真浏览器下成立。

### S4 example.com 点击链接 + 动作后复查 —— PASS（含一次假失败）

片段：见 §3-c。

| 运行 | `browser_click` 结果 | URL（动作后） | 复查 |
|------|----------------------|---------------|------|
| 运行 A（layer1-v3） | `success`, `resolution=ref`, `attempts=1` | `https://www.iana.org/help/example-domains` | `validate` → allowed，未拦 |
| 运行 B（layer1-v3b） | `error`, `error_type=timeout`, `attempts=3` + `js_fallback=true` | 同上（**已跳转**） | 同上，未拦 |

* 两次都证明**动作后复查工作**：`browser_open`/`browser_click` 之后 executor 拿新 URL 过策略（`allow_https` 放行公网 https，**跨域也不拦**——本用例从 example.com 跳到 iana.org）。
* 运行 B 是**假失败**：点击其实生效（URL 已到 iana.org），但 Playwright 的 click 在被导航带走的页面上等稳定 → 3 次尝试 + JS 兜底全超时 → 结构化 `timeout` 错误。详见 §5.2。

### S5 登录页检测（真实页面）—— PASS

片段：见 §3-d（每候选一个**新页**）。

```
{"tag":"s5_try","candidate":"https://www.zhihu.com/signin","seconds":8.0,"status":"success"}
{"tag":"s5_raw_detect","final_url":"https://www.zhihu.com/signin","html_len":51926,
 "has_visible_password":false,
 "intervention":{"kind":"login","confidence":"high","reason":"url-path:signin",
                 "human_prompt":"需要人工处理：检测到登录页面，请在浏览器中手动登录后继续任务。"}}
{"tag":"s5_executor_hitl","status":"success",
 "data":{... "needs_human":true, "intervention":"login",
         "human_prompt":"需要人工处理：检测到登录页面，请在浏览器中手动登录后继续任务。"}}
```

* 真实页面 HTML（51926 字符，按 `MAX_HTML_SAMPLE_CHARS` 截断前 20k）+ 密码框探测 → `detect_intervention` 命中 **`login` / `high` / `url-path:signin`**，提示文案可直接给用户。
* 同一页面经 **executor**（`browser_open`）走一遍：结果 `status=success` 且 `data.needs_human=true`、`data.intervention="login"` —— D5/D6 的 HITL 桥在真实页面成立。
* 边界（诚实说明）：zhihu 登录表单的密码框在首屏不可见（`has_visible_password=false`），所以命中的是 **URL 标记**，`password-input` 分支未在这条真实链路上被触发（该分支有单测覆盖）。
* **zhihu 之所以可达，是因为每个候选都用了新页**：第一版脚本复用同一页，第二个候选起全部报 "interrupted by another navigation to `chrome-error://chromewebdata/`"——即 §5.1 的池化页面竞态。

### S5b 任务书候选登录页可达性 —— SKIP（网络）

```
probe（每个候选一个新页，15s 上限）：
  https://www.zhihu.com/signin            success 1.6s
  https://passport.bilibili.com/login     success 1.1s
  https://passport.jd.com/new/login.aspx  success 4.7s
  https://login.taobao.com/member/login.jhtml success 7.6s（重定向 .../havanaone/login/login.htm）
  https://passport.csdn.net/login         success 11.0s
  https://weibo.com/login.php             success 7.2s（重定向 passport.weibo.com/visitor/...）
  https://github.com/login                timeout 15.0s   ← 任务书首选：本机网络不可达
  https://accounts.google.com/signin      timeout 15.0s   ← 任务书备选：同样不可达
```

* 任务书给的 `github.com/login`、`accounts.google.com/signin` 在本环境**都连不上**（`ERR_CONNECTION_TIMED_OUT`，非代码问题，属网络事实）→ 记录为 **SKIPPED (timeout)**，改用国内可达登录页完成检测验证（S5）。

### S6 失败导航后复用池页（异常复现）—— PASS（复现）

片段：见 §3-e。

```
{"tag":"s6_first_nav","target":"https://smoke-t6-does-not-exist.invalid/","status":"error",
 "error_type":"playwright_error","...":"net::ERR_NAME_NOT_RESOLVED"}
{"tag":"s6_reuse","same_page":true,"pages_created_delta":0}       # release → new_page 拿回同一页
{"tag":"s6_second_nav","target":"https://example.com","status":"error",
 "error_message":"... Page.goto: Navigation to \"https://example.com/\" is interrupted by another
                  navigation to \"chrome-error://chromewebdata/\" ..."}
{"tag":"s6_retry_after_1.5s","target":"https://example.com","status":"success",
 "url":"https://example.com/"}
```

* **确定性复现**（本次 1/1；此前在 S5 首版脚本里也自然出现 2/3 次）：一次失败导航后立刻 `release()` + `new_page()` 复用的页面，其**下一次 `goto` 会失败**，等 1.5s 重试即成功。详见 §5.1。

### S7 提交控件两路验证 —— PASS

```
{"tag":"refs","box":[["e6","searchbox","输入搜索词"]],"submit_like":[["e7","generic","搜索网页"]]}
{"tag":"click_submit","target":"e7","role":"generic","status":"success","attempts":1,"js_fallback":false}
{"tag":"after_click","url":"https://cn.bing.com/search?q=Python+3.13&search=&form=QBLH","navigated":true}
```

* 结论：bing 首页提交控件不是 `role=button`，但**按 ref 点击 `generic` 提交控件可以提交**；`keyboard Enter` 也能提交（另一次运行：`after_enter navigated=true 0.7s`）。
* 反面样本：点击**输入框自身**（e6）不会导航（8s 内 `q=` 不出现）—— ref 选错时动作层不会报错（点击输入框本身是合法动作），只能靠 URL 复查/二次快照发现。

### L2 真模型端到端（app + 真模型 + 真浏览器）—— PASS

片段：见 §3-f。任务（措辞必须命中 tier3，见 §5.4）：`在 www.bing.com 的搜索框里搜索 Python 3.13`

```
{"tag":"env","model":"deepseek/deepseek-chat","has_key":true,"key_len":35,
 "base_url":"https://api.deepseek.com/v1","browser_headless":true,"browser_max_pages":3}
{"tag":"pool_state","started":true,"channel":"msedge"}
{"tag":"http","status_code":200,"content-type":"text/event-stream"}
SSE：
  0 phase/start  {session_id: e8a45d51-…}
  1 answer       "已在 Bing 页面（https://cn.bing.com/），可以搜索“Python 3.13”。请确认是否继续执行在搜索框输入并搜索。"
  2 done         stop_reason=completed,
                 result_data={action:"navigate", requested_url:"https://www.bing.com",
                              url:"https://cn.bing.com/", status:200}
trace：classify decision {intent:"browser_task", tier:"tier3", confidence:1.0}
       execute tool_call {tool:"browser_snapshot", status:"error"}      ← 无当前页：no_open_page
       llm llm_call {kind:"structured"}
       execute tool_call {tool:"browser_open", status:"success"}
```

* 真模型 E2E 走通 tier3：规则分类命中 tier3（**classify 没花 LLM 调用**）、参数提取（1 次 LLM）→ `browser_open("https://www.bing.com")` 成功（重定向 cn.bing.com，HTTP 200），整轮 7.5s。
* 观察：链路在"打开页面"就结束，`done.stop_reason=completed` 而搜索**并未执行**（answer 让用户确认）——见 §5.3。

### L1.5 API 层 HITL：登录墙 → `human_pending` —— PASS

片段：见 §3-g（provider 固定返回登录 URL，因此**不依赖模型选择、不花模型调用**；浏览器仍是真 msedge）。

```
{"tag":"sse","index":0,"type":"phase","phase":"start","session_id":"c4dbadf7-…"}
{"tag":"sse","index":1,"type":"human_pending","session_id":"c4dbadf7-…",
 "human_prompt":"需要人工处理：检测到登录页面，请在浏览器中手动登录后继续任务。"}
{"tag":"hitl_assertion","human_pending":true,"no_answer_event":true,"seconds":3.6}
trace：classify decision {intent:"browser_task", tier:"tier3"} →
       execute tool_call browser_snapshot(error) → browser_open(success) → hitl node_start/node_end
```

* 任务书 Step 1 场景 2（登录墙 → `human_pending`）**在 API 层断言成立**：SSE 只发 `phase/start` + `human_pending`，**不发空 answer/done**（H2 契约），trace 里能看到 `hitl` 节点被进入。
* 未做：`/chat/resume` 恢复链路（P2 已覆盖；本次范围外）。

## 3. 可复现片段（短）

### a) Layer 1：池 / 导航 / 快照 / 输入 / 点击

```python
import asyncio, json
from browsepilot.app.browser import BrowserPool, page_snapshot_refs, parse_snapshot_refs
from browsepilot.app.browser.actions import click, navigate, type_text

async def main():
    pool = BrowserPool(headless=True, max_pages=5, channel="msedge")  # channel 必须：PW 1.59 默认 rev 1217 缺失
    await pool.start()
    page = await pool.new_page()
    print(json.dumps(await navigate(page, "https://example.com")))      # 期望 status=success
    refs = parse_snapshot_refs(await page_snapshot_refs(page))
    print([(r.label, r.role, r.name) for r in refs])                    # 期望 ref 数 > 0
    # bing：snapshot → 找 searchbox ref → type_text → 点提交 ref（或 keyboard Enter）→ 再快照
    r = await navigate(page, "https://www.bing.com")
    refs = parse_snapshot_refs(await page_snapshot_refs(page))
    box = next(x for x in refs if x.role == "searchbox")
    print(json.dumps(await type_text(page, box.label, "Python 3.13")))  # 期望 success, resolution.kind=ref
    sub = next(x for x in refs if x.name == "搜索网页")                  # 提交控件是 generic，不是 button
    print(json.dumps(await click(page, sub.label)))                     # 期望 success 且 URL 变成 /search?q=…
    print(page.url, len(parse_snapshot_refs(await page_snapshot_refs(page))))
    await pool.close()

asyncio.run(main())
```

### b) 策略门（含 executor 路径）

```python
from browsepilot.app.browser.policy import NavigationPolicy
from browsepilot.app.executor import AppToolExecutor
from browsepilot.app.services.weather import WeatherService
from browsepilot.app.services.search import SearchService
from browsepilot.app.services.fetch import PageFetchService

pol = NavigationPolicy()
for u in ("https://169.254.169.254/latest/meta-data/", "file:///C:/Windows/win.ini",
          "javascript:alert(1)", "https://example.com"):
    print(u, pol.validate(u))                    # 前三条 false + reason；最后 true
ex = AppToolExecutor(WeatherService(), SearchService(), PageFetchService(), browser=pool, policy=pol)
before = pool.pages_created
print(await ex.call("browser_open", {"url": "file:///C:/Windows/win.ini"}))  # navigation_blocked
print("pages_created_delta:", pool.pages_created - before)                   # 期望 0
```

### c) 点击 + 动作后复查

```python
r = await ex.call("browser_open", {"url": "https://example.com"})
snr = await ex.call("browser_snapshot", {})
refs = parse_snapshot_refs(snr.data["refs"])
link = next(x for x in refs if x.role == "link")
cr = await ex.call("browser_click", {"ref": link.label})
print(cr.status, cr.error_type, cr.data.get("url"))      # 期望 URL = iana.org；可能 success 或 timeout(假失败)
print(pol.validate(cr.data.get("url", "")))              # 动作后复查：期望 (True, "")
```

### d) 登录页检测（真实页面）

```python
from browsepilot.app.browser.actions import navigate as nav
from browsepilot.app.browser.detect import detect_intervention

page2 = await pool.new_page()                    # 每个候选都要新页（见 §5.1）
print(await nav(page2, "https://www.zhihu.com/signin"))
html = await page2.content()
pw = await page2.locator("input[type=password]:visible").count()
print(detect_intervention(page2.url, html[:20000].lower(), pw > 0))
# 期望 Intervention(kind="login", confidence="high", reason="url-path:signin", human_prompt=…)
print(await ex.call("browser_open", {"url": "https://www.zhihu.com/signin"}))
# 期望 data.needs_human=True, data.intervention="login"
```

### e) 池化页面复用异常复现（S6）

```python
p = await pool.new_page()
print(await nav(p, "https://smoke-t6-does-not-exist.invalid/"))   # error: ERR_NAME_NOT_RESOLVED
await pool.release(p)
p2 = await pool.new_page()                                        # 复用同一页
print("same page:", p2 is p)
print(await nav(p2, "https://example.com"))                       # error: interrupted by … chrome-error://
await asyncio.sleep(1.5)
print(await nav(p2, "https://example.com"))                       # success
```

### f) Layer 2 E2E（in-process app + `/chat/stream`）

```python
# 前置：export BP_MODEL_API_KEY=$(grep '^OPENAI_API_KEY=' .env | cut -d= -f2-)   # 仅进程内
#       export BP_MODEL_BASE_URL=$(grep '^OPENAI_BASE_URL=' .env | cut -d= -f2-)
from fastapi.testclient import TestClient
from browsepilot.app.config import Settings
from browsepilot.app.main import create_app
from browsepilot.app.executor import AppToolExecutor
from browsepilot.app.browser.browser_pool import BrowserPool
from browsepilot.model.litellm_provider import LiteLLMProvider

settings = Settings(checkpoint_db_path=..., session_db_path=..., trace_dir=...)  # 全部指到临时目录
pool = BrowserPool(headless=True, max_pages=5, channel="msedge")   # _default_app() 没有 channel 开关，见 §5.6
provider = LiteLLMProvider(settings.default_model,
                           api_key=settings.model_api_key or None,
                           base_url=settings.model_base_url or None)
app = create_app(settings, provider=provider,
                 executor=AppToolExecutor(WeatherService(), SearchService(), PageFetchService(),
                                          browser=pool),
                 tracer=JsonlTracer(trace_id="t6-e2e", out_dir=tmp), browser_pool=pool)
with TestClient(app) as client:            # with 才会跑 lifespan（pool.start）
    with client.stream("POST", "/chat/stream",
                       json={"task": "在 www.bing.com 的搜索框里搜索 Python 3.13"}) as resp:
        events = [json.loads(l[6:]) for l in resp.iter_lines() if l.startswith("data: ")]
# 期望 events 类型: ["phase", "answer", "done"]，done.result_data.url = https://cn.bing.com/
```

### g) Layer 1.5 HITL（无模型也可跑）

同一 `create_app` 骨架，但 `provider` 用确定性替身（`structured()` 对含 `url` 字段的 schema 返回
`schema(url="https://www.zhihu.com/signin")`），任务用 `"在 zhihu.com 的搜索框里搜索 Python 3.13"`：
期望 SSE `["phase", "human_pending"]`，且 `human_pending.human_prompt` 含"登录"。

## 4. 跳过项与原因（诚实清单）

| 跳过 | 原因 |
|------|------|
| `github.com/login`、`accounts.google.com/signin` | 本机网络 15s 超时（`ERR_CONNECTION_TIMED_OUT`），非产品问题；改用可达登录页验证检测 |
| 真实**验证码**页 / **确认弹窗**页 | 没有找到稳定、可重复触发的真实页面；不刻意构造（构造的页面会变成"测试自己写的假页面"，不算真实冒烟）。这两个分支由单测覆盖，真实链路未触发 → SKIP |
| `password-input` 分支真实触发 | 可达登录页首屏密码框不可见（`has_visible_password=false`），命中路径是 `url-path:signin` |
| `/chat/resume` 人工恢复后继续 | P2 已覆盖；本次范围外 |
| 前端（Vue）HITL 交互 | 仅断言 API 层（任务书允许"前端或 API 层"） |
| 跨域/多会话并发下池行为 | 单会话冒烟，未做并发压测 |

## 5. 异常与给 T8/P4 的提示（本次不改代码）

### 5.1 池化页面的"错误页竞态"（S6，确定性复现）

* 现象：一次失败的 `goto`（如 DNS 失败）之后，页面会自行导航到 `chrome-error://chromewebdata/`；此时立刻 `release()` 回池，下一个 `new_page()` 拿到的还是**同一页**，它紧接着的 `goto` 报
  `Navigation to "…" is interrupted by another navigation to "chrome-error://chromewebdata/"`；等 1.5s 重试成功。
* 影响：**一次失败导航可能连累下一次 `browser_open`**（假失败 → 图里多一条 degradation）。在 S5 首版脚本里表现为 3 个候选连续失败，改用"每候选新页"后全部可达——说明这是页面状态问题而非站点问题。
* 建议（P4/T8）：`BrowserPool.release()`/`new_page()` 复用前做一次页面复位（`goto("about:blank")` 或短 `wait_for_load_state`），或在 `actions.navigate` 失败路径显式等待错误页落定；也可给 `browser_open` 加一次同参数重试。

### 5.2 点击后被跨页导航带走 → 假失败（S4 运行 B）

* 现象：`browser_click` 到 `success`（运行 A）或 `error/error_type=timeout`（运行 B，`attempts=3` + `js_fallback=true`），但两次 **URL 都已到 `https://www.iana.org/help/example-domains`**——动作其实生效了。
* 影响：动作层把"点击成功但页面在导航中"报成失败；executor 按契约保留错误（"失败即数据"），图可能重放/多绕一步。
* 建议（P4）：`status=error` 但**动作后 URL 已变化**时按"动作已生效"处理（或在 click 上用 `no_wait_after` / 更短的稳定性检查），并把这一判定写进 trace。

### 5.3 tier3 策略链只到"打开页面"（L2 观察）

* `graph._STRATEGY_CHAIN["browser_task"] = ["browser_snapshot", "browser_open"]`：交互类任务（"在搜索框里搜索 X"）在 `browser_open` 成功后即返回，**不会**执行 `browser_type`/`browser_click`；answer 文案变成"可以搜索…请确认是否继续"，而 `done.stop_reason=completed`。
* 影响：对外表现为"完成了"，实际只完成了第一步（P4 若要真正完成交互任务，需要把 type/click 纳入链或改成步骤化计划）。
* 本次不改代码，仅记录（任务书禁令）。

### 5.4 任务书示例任务会走到 tier2，不走 tier3

* `router.classify_by_rules` 先做 `_URL_RE.search`（`http(s)://…` → 直接 tier2 read_page），且 `read_page` 关键词含"打开/访问/读/阅读/摘要"，都排在 `browser_task` 之前。
* 所以任务书 Layer 2 的示例 "打开 example.com 看看标题" 会命中 tier2（`fetch_page` 成功就结束，浏览器不参与）。
* 本次 tier3 冒烟的措辞选了"在 www.bing.com 的搜索框里搜索 …"（命中强触发词"搜索框"，且不含 `http://`、不含 read_page 关键词）→ 实测 tier3 命中、无需 LLM 分类。
* 给 T8：tier3 的 E2E 用例措辞要照这个约束写，否则测的其实是 tier2。

### 5.5 提交控件可能不是 `role=button`（S2/S7）

* bing 首页提交按钮在无障碍树里是 `generic "搜索网页"`（`button_refs` 为空）；点击该 ref 有效（0.2s），点击输入框自身则不会提交。
* `actions.py` 目前**没有键盘/Enter 原语**（`click`/`type_text`/`scroll`/`navigate`）。本次用"按 ref 点击 generic 提交控件"绕开；遇到只能键盘提交（或 IME/搜索建议）的页面时，模型没有可用动作。
* 建议（P4）：补 `press`/`submit`（或 `type_text(..., submit=True)`）能力，并在 prompt/技能里说明"提交控件可能不是 button"。

### 5.6 生产接线缺口：`.env` 的 `BROWSER_CHANNEL` 没接线（本机后果最重）

```
{"tag":"settings","browser_headless":true,"browser_max_pages":3,
 "has_browser_channel_field":false,"env_file_browser_channel":"<unset>"}
{"tag":"no_channel_start","started":false,
 "error":"BrowserType.launch: Executable doesn't exist at …\\chromium_headless_shell-1217\\…\\chrome-headless-shell.exe"}
{"tag":"msedge_start","started":true}
```

* `browsepilot.app.config.Settings` **没有** `browser_channel` 字段（`env_prefix="BP_"` + `extra="ignore"` ⇒ `.env` 里的 `BROWSER_CHANNEL=msedge` 被静默忽略），`main._default_app()` 构造池时也不传 `channel`。
* 后果（本机）：`uvicorn browsepilot.app.main:app` 的 `BrowserPool.start()` 会失败（PW 1.59 要 chromium rev 1217，缓存只有 1208）→ lifespan 诚实降级（Tier3 全部工具返回 `browser_unavailable`），服务不崩但也**没有浏览器能力**。
* 修复方向（T8/P4，非本任务）：`Settings` 增加 `browser_channel: str = ""` 并在 `_default_app()` 传入（`channel=settings.browser_channel or None`）；或在部署镜像里执行 `playwright install chromium`。
* 本记录里所有真浏览器场景都显式用了 `channel="msedge"`（这也是 T1/T2/T4 的既有实践）。

## 6. 逐条对应任务书

| 任务书要求 | 记录位置 | 结果 |
|------------|----------|------|
| 池启动 + example.com + ref 数 > 0 | §2 S1 | PASS（5 refs） |
| bing 搜索：snapshot→type→提交→URL 变化 + 结果 refs | §2 S2 + §2 S7 | PASS（`/search?q=Python+3.13`，30 refs） |
| 策略门：metadata IP 拦截；`file://` 经 executor 路径拦截 | §2 S3 | PASS（`pages_created_delta=0`） |
| 登录页 `detect_intervention` 记录 kind + prompt | §2 S5 | PASS（login/high/url-path:signin） |
| 点击链接 + 动作后复查放行 | §2 S4 | PASS（iana.org，未拦；含 §5.2 假失败观测） |
| Layer 2 真模型 E2E（SSE 事件序列） | §2 L2 | PASS（phase/answer/done，7.5s；并附一致性更好的 HITL 断言见 L1.5） |
| 无模型 key → 记 SKIPPED | — | 不适用：`.env` 有可用 key（本文档不含 key） |
| 只提交本文档 | §7 | 已遵守 |

## 7. 交付与提交

* 唯一提交文件：`browsepilot/docs/v2-p3-smoke.md`（`git add` 仅此文件；`src/`、测试、`.env` 未改）。
* 冒烟脚本位置（未入库、可删除）：`%TEMP%\t6-smoke\{layer1_v3.py, probe_login.py, s7_submit.py, layer15_hitl.py, layer2_e2e.py, check_default_wiring.py}` 及对应 `.log`。
* 密钥：`BP_MODEL_API_KEY` 仅在 shell 进程内导出使用，未落盘；本文件与日志中均无 key/token。
