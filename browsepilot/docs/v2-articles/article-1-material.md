# 文章 1 素材：《从"最短路径"到"工程化"：一次 Vibe Coding 的重构实录》

> 采集时间：2026-08-03（P0 T12，"before" 状态最新鲜时）
> 文章主题：快速原型 → 技术债的真实经历，对比 AI 最短路径代码与工程化实践差距

## 叙事线

v0.1 用 Claude Code + DeepSeek API 快速堆出 7 节点 StateGraph
（classify → pre_observe → plan → execute → reflect → replan → answer）→ 运行脆弱 →
技术债清单（设计文档 §1.2 的 11 条根因）→ 决定 V2.0 重构（分层 + 端口 + Pydantic + 结构化输出）。

## 事实数据（2026-08-03 采集）

### 上帝文件规模

```bash
wc -l backend/app/main.py backend/app/agent/nodes.py backend/app/agent/graph.py backend/app/session_manager.py
```

| 文件 | 行数 |
|------|------|
| backend/app/main.py | 529（路由+SSE+状态重建+持久化全揉一起） |
| backend/app/agent/nodes.py | 1316（7 节点核心逻辑） |
| backend/app/agent/graph.py | 117 |
| backend/app/session_manager.py | 369 |
| 合计 | 2331 |

### 熔断阈值（"3 次失败就放弃"）

- `backend/app/config.py:57` — `consecutive_failures_threshold: int = 3`
- `backend/app/agent/nodes.py:983-986` — 连续 3 步失败 → `stop_reason: "连续 3 步执行失败，为避免无限重试，按照现有搜集到的材料组织回答"`

→ 用户看到的结果：答非所问的降级回答，而不是"问题暴露 + 换策略"。

### 域名白名单（P0 失败根因 #1）

- `backend/app/config.py:39` — `allowed_domains: str = "github.com,baidu.com,wikipedia.org"`
- `browser_mcp/main.py:15` — 启动时 `set_allowed_domains(...)` 注入 MCP 层
- `browser_mcp/tools/navigate.py:14` — 导航前 `validate_url(url)` 拦截白名单外域名
- **后果**：weather.com.cn、jd.com、taobao.com、ctrip.com 从架构上就不可能访问

### 等待策略（根因 #2）

- `browser_mcp/tools/navigate.py:19` — `page.goto(url, wait_until="domcontentloaded")`
- `browser_mcp/tools/click.py:57` — 点击后 `wait_for_load_state("domcontentloaded")` 3 秒超时
- **后果**：JS 渲染页面元素未就绪就去点击 → 失败

### 结构化输出靠"猜"（根因 #9）

- `backend/app/agent/nodes.py:31` — `extract_json()`（正则硬解析）
- `backend/app/agent/nodes.py:55` — `repair_json()`（修补非法 JSON）
- `backend/app/agent/state.py:14` — `degradation_log` 含 `"reason": "json_parse_failed"`

### 旧代码提交历史（V2 之前的最后 10 条）

```
64b84fe docs: 更新 CLAUDE.md 为中文版 + 重写 README.md
ded5fe1 fix: increase arg-filling max_retries to 1, add ref hint for click/type
436c28a fix: auto-capture screenshot after page-modifying operations in execute_node
6a0042e feat: Skill系统 + HITL登录验证 + Playwright MCP迁移
01542c2 refactor: remove get_tools_schema() and build_tools_from_mcp()
ad58f40 fix: Chinese prompts + navigation decision overhaul
a88b965 fix: inject current date into plan and answer node prompts
155f8e4 fix: pass meta to pre_observe_node tool calls
0793ba2 fix: guard result.data unpacking when it is None
d830f5b fix: restore MCP client retry with exponential backoff in connect()
```

（提交主题几乎全是 "fix:"——修复驱动的开发，印证"运行脆弱 → 打补丁"的循环）

### V2 基线（重构起点）

- 旧测试：66 passed（2026-08-03，`uv run pytest tests/ -q`）
- 旧应用入口：backend/app/main.py（FastAPI :8000）、browser_mcp/main.py（MCP :9400）
- 基线文档：`docs/v2-baseline.md`

## 对比表草稿：最短路径 vs 工程化

| 维度 | AI 最短路径（v0.1） | 工程化实践（V2） |
|------|--------------------|-----------------|
| 代码组织 | 上帝文件（main.py 529 行 / nodes.py 1316 行） | 三层单向依赖 model ← agent ← app |
| 状态管理 | 25+ 字段扁平 TypedDict + main.py 手工重建 | Pydantic v2 状态模型（AgentState） |
| LLM 输出 | 正则 extract_json / repair_json 兜底 | forced tool calling + Pydantic 校验 + 重试 |
| 工具接入 | 直接绑定 MCP 生命周期 | ToolExecutor 端口，MCP 只是实现之一 |
| 可观测性 | 无 | Tracer 端口 + JSONL sink |
| 错误处理 | 熔断 = 放弃（3 次失败强制回答） | 重试 → 换策略 → 暴露问题 → HITL → 降级 |
| 依赖方向 | 无约束 | 架构守卫测试（AST 扫描 import）持续强制 |

## 金句素材

- "天气搜索失败不是 bug，是范式问题——让 LLM 徒手扒 DOM。"
- "每个站点结构不同，复杂站必然崩——这是概率问题，不是运气问题。"
- "提交历史里满是 'fix:'——修复驱动的开发是技术债的体温计。"

## 代码位置索引（供引用）

| 内容 | 位置 |
|------|------|
| 旧熔断 | backend/app/config.py:57、nodes.py:983 |
| 旧白名单 | backend/app/config.py:39、browser_mcp/main.py:15、tools/navigate.py:14 |
| 旧等待 | browser_mcp/tools/navigate.py:19、click.py:57 |
| 旧 JSON 兜底 | backend/app/agent/nodes.py:31、55 |
| V2 结构化输出 | src/browsepilot/model/structured.py |
| V2 状态模型 | src/browsepilot/agent/state.py |
| V2 架构守卫 | tests/test_architecture.py |
| 评测标尺 | eval/scenarios/*.yaml |
