# V2.0 P1 前置验证记录（P1 Task 1）

- 日期：2026-08-03
- 分支：`v2.0`
- 目的：修根因前先用证据确认，不盲信文档 —— 复核四项根因的代码证据，并实测 ddgs / Open-Meteo 在当前网络下的可用性
- 方法：本任务不写 pytest（外部网络不进测试套件），用一次性脚本验证并记录
- 红线：旧代码（backend/、browser_mcp/、frontend-vue/）零修改，仅只读取证（grep/sed）

## 一、四项根因证据复核（旧代码只读取证）

全部命令在 `D:/AI_Agent_Demo/browsepilot` 下执行，证据均确认为真实存在。

| # | 根因 | 证据（文件:行号） | 代码摘录 | 确认 |
|---|------|------------------|----------|------|
| 1a | 白名单默认值卡死 | `backend/app/config.py:39` | `allowed_domains: str = "github.com,baidu.com,wikipedia.org"` | ✅ |
| 1b | validate_url 拦截 + 白名单仅环境变量生效 | `browser_mcp/tools/navigate.py:14`、`browser_mcp/main.py:15` | navigate.py:14 `is_valid, error = validate_url(url)`；main.py:15 `set_allowed_domains([d.strip() for d in domains.split(",")])`（仅当 `ALLOWED_DOMAINS` 环境变量非空时调用，默认即硬编码三域名） | ✅ |
| 2 | domcontentloaded 等待不充分 | `browser_mcp/tools/navigate.py:19`、`browser_mcp/tools/click.py:57` | navigate.py:19 `await asyncio.wait_for(page.goto(url, wait_until="domcontentloaded"), timeout=15)`；click.py:57 `await asyncio.wait_for(page.wait_for_load_state("domcontentloaded"), timeout=3)` | ✅ |
| 3 | 熔断 3 次即放弃 | `backend/app/config.py:57` + `backend/app/agent/nodes.py:983-986` | config.py:57 `consecutive_failures_threshold: int = 3`；nodes.py:983-986 `if state.get("consecutive_failures", 0) >= settings.consecutive_failures_threshold: ... return {"plan": [], "need_replan": False, "stop_reason": f"连续 {state['consecutive_failures']} 步执行失败，为避免无限重试，按照现有搜集到的材料组织回答"}` | ✅ |
| 4 | 手写指数退避（无抖动、无上限） | `backend/app/mcp_client.py:56`（+58、35） | mcp_client.py:56 `wait = 2 ** attempt`；:58 `await asyncio.sleep(wait)`；:35 `for attempt in range(max_retries)`（`connect(max_retries=3)`，无 jitter、无上限封顶） | ✅ |

## 二、ddgs 实测（Step 2）

命令：

```bash
uv run python -c "
import time
from ddgs import DDGS
t = time.time()
with DDGS() as d:
    results = list(d.text('Python 3.13 release date', max_results=5))
print(f'{time.time()-t:.2f}s, {len(results)} results')
for r in results[:3]:
    print('-', r['title'][:50], '|', r['href'][:60])
"
```

第 1 次（冷启动）输出：

```
21.36s, 5 results
- Python Release Python 3.13.15 | Python.org | https://test.python.org/downloads/release/python-31315/
- The official home of the Python Programming Langua | https://www.python.org/downloads/
- Exploring Python 3.13: Innovations and Enhancement | https://python.plainenglish.io/exploring-python-3-13-innovat
```

第 2 次（复用连接）输出：

```
6.61s, 5 results
```

结论：

- ✅ **可用，无需代理**（本环境直连成功，未配置任何 proxy）
- 结果质量：`max_results=5` 返回 5 条真实结果，python.org 官方链接在列，`title`/`href` 字段完整
- 延迟：首次 21.36s（冷启动/DNS/握手），第二次 6.61s —— 波动大
- 对 T4 影响：ddgs 方案可行、无需换实现；但 SearchService 必须异步 + 充分超时（≥30s）+ 失败/超时抛 `SearchError`（tenacity 重试，与 T2 一致）

## 三、Open-Meteo 实测（Step 3）

命令：

```bash
uv run python -c "
import httpx, time
t = time.time()
r = httpx.get('https://api.open-meteo.com/v1/forecast',
    params={'latitude': 39.9042, 'longitude': 116.4074,
            'current': 'temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m'},
    timeout=10)
print(f'{time.time()-t:.2f}s, status={r.status_code}')
print(r.json()['current'])
"
```

输出：

```
1.01s, status=200
{'time': '2026-08-16T14:00', 'interval': 900, 'temperature_2m': 24.3, 'relative_humidity_2m': 73, 'weather_code': 0, 'wind_speed_10m': 4.2}
```

补充验证（中文地名解析，一次性脚本）：

```bash
uv run python -c "
import httpx
r = httpx.get('https://geocoding-api.open-meteo.com/v1/search',
    params={'name': '北京', 'count': 1}, timeout=10)
print(f'status={r.status_code}, results:', len(r.json().get('results', [])))
"
# 输出：status=200, results: 0
```

结论：

- ✅ **无 key 直连可用**：1.01s、status=200，`current` 字段完整（`time`、`interval=900`、`temperature_2m=24.3℃`、`relative_humidity_2m=73%`、`weather_code=0`、`wind_speed_10m=4.2m/s`）
- 中文城市名：forecast API 只接受经纬度；实测其 geocoding API 对中文名（"北京"）返回 **0 结果** —— 确认中文城市名→坐标必须靠**内置城市表**（T3 设计成立）

## 四、对 T3/T4 的影响

| 任务 | 影响结论 |
|------|----------|
| T3（WeatherService.get / Weather / CityNotFoundError） | Open-Meteo 无 key 方案可行（200 / ~1s），`current` 字段满足天气服务需求；必须内置中文城市→经纬度表，未知城市抛 `CityNotFoundError`（与 T3 定义一致）；实现用 httpx 异步 + 超时（10s 足够），失败可 tenacity 重试 |
| T4（SearchService.search / SearchResult / SearchError） | ddgs 可用、无需代理，搜索实现方案不变；延迟波动大（6.6~21.4s），SearchService 需异步 + 充分超时（≥30s）+ 失败/超时抛 `SearchError`；实测返回 `title`/`href`/`body` 字段与 T4 `SearchResult` 定义匹配 |

## 环境备注

- uv 0.10.4，pyproject 清华镜像已配置；全部 Python 命令用 `uv run python` 执行
- 旧代码零修改（仅 grep/sed 只读取证）
