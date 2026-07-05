# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在此仓库中工作时提供指导。

## 常用命令

```bash
# 运行所有测试
cd browsepilot && .venv/Scripts/python -m pytest tests/ -v

# 仅检查后端导入
cd browsepilot && .venv/Scripts/python -c "from backend.app.main import app; print('OK')"

# 一键启动所有服务 (Windows):
cd browsepilot && start.bat

# 手动启动服务（三个终端，均在 browsepilot/ 目录下）:
.venv/Scripts/python -m browser_mcp.main          # MCP 服务器 :9400
.venv/Scripts/python -m uvicorn backend.app.main:app --port 8000  # 后端 :8000
cd frontend-vue && npm run dev                     # 前端 :5173
```

**Python 必须使用 Python 3.14**，路径为 `C:\Users\lzy\AppData\Local\Programs\Python\Python314\python.exe`。Windows Apps 别名（`python3`）存在沙箱问题。

**pip 镜像源**：pip（阿里云）和 uv（阿里云）均已全局配置。

## 架构

三个服务，自上而下：

**前端** (`frontend-vue/`) — Vue 3 + Pinia + TypeScript + Element Plus + Tailwind CSS。通过 `/chat/stream` 进行 SSE 流式传输。Store 文件：`chat.ts`（消息、token、阶段）、`session.ts`（侧边栏列表、重命名/置顶）。使用 `marked` 进行 Markdown 渲染。

**后端** (`backend/app/`) — FastAPI + LangGraph。Agent 是一个 7 节点的 StateGraph：`classify → pre_observe → plan → execute → reflect → replan → answer`。`nodes.py` 是核心（约 1100 行），`graph.py` 定义边和路由条件。

**browser-mcp** (`browser_mcp/`) — 由 `USE_PLAYWRIGHT_MCP` 环境变量控制两种模式：
- **自建模式**（默认）：MCP 服务器（Streamable HTTP），由 `BrowserPool` 管理 Playwright Chromium 实例。`tools/` 中有 8 个工具。端口由 `MCP_SERVER_PORT` 环境变量指定（默认 8091）。
- **Playwright MCP 池模式**：`PlaywrightMCPPool` 管理 Node.js `@playwright/mcp` 子进程。端口从 9400 开始。

`mcp_client.py` 封装 `fastmcp.Client` 供 agent 使用。快照数据在边界处截断（仅保留 ref 行，最多 30 条），防止上下文膨胀。

### Agent 状态 (`backend/app/agent/state.py`)

AgentState 是一个 TypedDict，包含 messages、task、plan、execution_log、token_usage 以及控制字段（retry_count、need_replan、stop_reason 等）。标量字段使用默认的 "replace" reducer；仅 `messages` 使用 `add_messages`。

### 双模型架构

Agent 使用两个 LLM 模型以平衡成本与质量：
- **小模型**（`SMALL_MODEL`，默认 `deepseek-chat`）— 仅用于 classify_node（意图识别）
- **大模型**（`BIG_MODEL`，默认 `deepseek-v4-flash`）— 用于 plan、execute、reflect、replan、answer

每个模型有独立的 API key 和 base URL 配置（回退到 `OPENAI_API_KEY`/`OPENAI_BASE_URL`）。

### 分类与路由

`classify_node` 使用小模型将用户输入分为三种意图：
- **chitchat**（闲聊）→ 直接回答（路由到 answer_node）
- **knowledge_qa**（知识问答）→ 直接回答（路由到 answer_node）
- **browser_task**（浏览器任务）→ pre_observe → plan → execute → reflect 循环

### pre_observe 节点

在规划之前，`pre_observe_node` 导航到目标页面并捕获页面结构。它从任务中提取 URL（直接匹配或"去X上搜索"模式），导航到该页面，并将 `page_structure` + `page_screenshot` 存入状态。这为 `plan_node` 提供了真实的 DOM 上下文用于步骤规划。

### Skill 系统 (`backend/app/agent/skill_loader.py`)

Skill 是 `skills/<name>/SKILL.md` 目录下的 Markdown 文件，带有 YAML 前置元数据（`name`、`description`、`trigger_keywords`）。两层渐进式披露：
1. **列表层**：每次 plan 提示词中注入所有 skill 的名称和描述（token 成本极低）
2. **完整内容层**：当触发模式匹配用户任务时，注入匹配 skill 的完整内容

示例：`skills/ecommerce-compare/SKILL.md` 在匹配"比价"/"购物"/"京东"等关键词时触发，提供特定领域的操作规范。

### HITL（人机协同）

Agent 可以暂停并请求人工干预（登录、验证码、确认）：
- `execute_node` 在检测到登录页面或验证码时调用 LangGraph 的 `interrupt()`
- 在状态中设置 `pending_human_action=true` + `human_prompt`
- 前端显示提示信息；用户手动完成操作
- 前端调用 `POST /chat/resume`（携带 session ID）继续执行
- `hitl_triggered_count` 防止恢复后重复触发

### API 端点

| 端点 | 方法 | 描述 |
|----------|--------|-------------|
| `/chat/stream` | POST | Agent 主执行流程，SSE 流式返回 |
| `/chat/resume` | POST | 恢复暂停的（HITL）会话 |
| `/history/{session_id}` | GET | 完整会话历史，包含所有轮次 |
| `/replay/{session_id}` | GET | 回放特定轮次的执行步骤 |
| `/sessions` | GET | 列出所有会话（置顶优先） |
| `/sessions/{id}` | DELETE | 删除会话 |
| `/sessions/{id}/rename` | PATCH | 重命名会话 |
| `/sessions/{id}/pin` | PATCH | 切换会话置顶状态 |
| `/health` | GET | 健康检查 |

### 关键流程：execute_node

1. 构建工具描述 + recent_context（页面结构 + 工具调用历史）
2. **评分分类器**（`classify_tool`）— 关键词 (+1) + 正则 (+2) 对 8 个工具进行评分，阈值 = 2
3. **参数提取**（`extract_args`）— 基于正则提取 navigate（URL）、type_text（文本），screenshot/get_content/get_page_structure 无需参数。返回 `(args_dict, is_complete)`
4. 分类器命中 + 参数完整 → 完全跳过 LLM
5. 分类器命中 + 参数缺失/不完整 → LLM 补全剩余参数。`REQUIRES_ARGS` 检查防止 click/type_text/navigate/execute_script 的参数为空。失败时回退到完整 LLM 选择
6. 分类器未命中 → 完整 LLM 工具+参数选择

### 步骤限制（双层）

`plan_step_count`（每次 plan/replan 重置为 0，上限 **15**）跟踪当前计划内的步骤。`total_steps`（永不重置，上限 **30**）是全局计数。两者在 `reflect_node` 中以 OR 条件检查。

### 启发式检查（`_run_heuristic_checks`）

对最后执行步骤进行零 LLM 成本检查：content_short（仅 get_content）、domain_mutation、similar_results、few_elements、click_no_navigation（导航点击后 URL 未变化）、mcp_validation_error。

### Token 追踪

`nodes.py` 中的 `accumulate_tokens()` 记录每个节点的 token 消耗。Answer 流式 tokens 在 main.py 中从 `llm.astream()` 捕获——如果最后一个 chunk 没有 `usage_metadata`，则使用估算值，前端显示"(估算)"。

### 熔断与安全限制 (`config.py`)

多种独立阈值防止 agent 循环失控，均可通过 `.env` 配置：
- **连续失败**（`consecutive_failures_threshold=3`）：MCP 工具连续出错 → 强制回答
- **停滞检测**（`stagnation_threshold=3`）：连续相同结果 → 强制回答
- **重新规划次数**（`replan_max_count=2`）：每次会话最多重新规划次数 → 强制回答
- **完成检查**（`completion_check_count`）：每次会话最多 1 次完成检查
- **递归警告**（`recursion_warning_threshold=10`）：图转换达到 N 次时记录警告日志
- **会话超时**（`session_timeout_seconds=300`）：每次会话最大实际耗时
- **上下文限制**（`max_context_tokens=8000`、`max_messages_count=50`）：截断提示词上下文

步骤限制（`plan_step_count <= plan_step_limit`、`total_steps <= total_step_limit`）在 `reflect_node` 中检查，由它先设置 `stop_reason` 再进行路由。

### 会话持久化

`SessionManager` 持久化到 `data/sessions/{id}.json`。一个会话包含多个"轮次"——每个轮次有自己的 task、execution_log、final_answer 和 token_usage。`list_sessions` 先按置顶排序，再按创建时间排序。会话级别的字段（custom_name、pinned）在重新持久化时保留。

## 会话分析

```bash
# 快速概览（jq，已自动授权）
jq -r '.execution_log | to_entries[] | "\(.key): \(.value.step[:50]) | \(.value.tool) | \(.value.result.status // "?")"' browsepilot/data/sessions/SESSION_ID.json

# 详细输出（python，需要确认）
cd browsepilot && .venv/Scripts/python -c "
import json
with open('data/sessions/SESSION_ID.json', encoding='utf-8') as f:
    d = json.load(f)
for i, e in enumerate(d['execution_log']):
    print(f'{i}: {e[\"step\"][:50]} | {e[\"tool\"]} | {e[\"result\"].get(\"status\",\"?\")}')
"
```

## 工具偏好

在临时数据探索中优先使用内置工具而非 `python -c "..."`——内置工具不会触发权限提示：

| 任务 | 使用 |
|------|-----|
| 读取文件内容 | `Read` |
| 搜索代码/文本 | `Grep` |
| 按文件名查找 | `Glob` |
| 解析/过滤 JSON | `jq`（已自动授权） |
| 编辑后语法检查 | 由 PostToolUse 钩子自动触发 |
| 导入/健康检查 | 使用上述"常用命令"中的确切命令 |

`python -c` 仍然适用于复杂的多步数据转换，但 80% 的单行命令可以用 Grep/Read/jq 替代。

## 注意事项

- `.claude/` 目录在 `.gitignore` 中。`docs/superpowers/` 中的计划文件不受 git 跟踪。
- `browsepilot/.env` 被 gitignore，但启动后端必须存在（包含 `OPENAI_API_KEY`）。完整模板见 `.env.example`。
- `mcp_settings.json` 是 MCP 服务器 URL 的唯一配置来源。任何 Python/TypeScript 文件中都不应有硬编码的端口。
- 调试 agent 行为时，`data/sessions/` 中的会话 JSON 比 `browsepilot.log` 更有价值——它包含完整的 execution_log、token 明细和 stop_reason。
- `graph.py` 中的 `_route_reflect` 函数不应包含步骤限制检查——这些检查属于 `reflect_node`，由它先设置 stop_reason 再进行路由。
- 收到请求时，先讨论解决方案和计划，而不是立即开始编码。
- 调试 CSS 布局问题（溢出、滚动、元素尺寸）时，始终从根容器开始，逐层向下追踪整个父级 grid/flex 高度链——在修改任何单个组件之前先检查所有中间容器。注意检查那些会打断高度继承链的 auto-sized 行/列。
- 推送到 GitHub 之前，确认：(1) README.md 在仓库根目录，(2) 清理过期的 .md 文件和文档目录，(3) .gitignore 排除了数据目录、环境变量文件和生成输出。
- 使用 `browsepilot/` 下的 `start.bat`（Windows）或 `start.sh`（Linux/Mac）一键启动所有三个服务——它会处理端口清理、venv/node_modules 检查和启动顺序。
- `browser_mcp` 模块由 `USE_PLAYWRIGHT_MCP` 环境变量控制两种模式。Playwright MCP 池模式会启动 Node.js 子进程；自建模式使用纯 Python Playwright。配置（`.env`）和 `mcp_settings.json` 必须就使用哪种模式保持一致。

## 经验教训（2025-05-25 Playwright MCP 迁移）

这些是从一次严重出错的迁移中总结的惨痛教训。在进行任何基础设施级别的变更之前请重新阅读。

### 1. 单元测试 ≠ 可工作的系统

所有测试都使用 `AsyncMock` 模拟 MCP 客户端。没有任何测试连接过真实的 MCP 服务器、真实的浏览器，或验证过端到端的数据流。当迁移破坏了 `call_tool`（读取 `result.data` 而非 `result.content`）时，全部 66 个测试仍然通过。

**规则**：在 MCP 层、数据格式或工具接口发生任何变更后，运行一次**手动 E2E 冒烟测试**——发送一个真实任务通过整个系统，验证浏览器执行了正确的操作。30 秒的手动测试就能发现所有花了 6 小时调试的问题。

### 2. 外部服务返回格式：在边界处验证，而非下游

Playwright MCP 返回的数据在 `result.content` 中（一个 `TextContent`/`ImageContent` 对象列表），而不是 `result.data`（始终为 `None`）。旧的自建工具返回数据在 `result.data` 中。`MCPClient.call_tool()` 从未针对这个差异进行更新，因此每次工具调用都向 agent 返回了空数据。

**规则**：替换服务层时，最先要检查的是代码接触外部服务处的返回格式。阅读实际的响应对象，不要假设它们与旧格式一致。

### 3. 代理/挂载模式会破坏会话状态

`fastmcp` 的 `create_proxy` + `mount` 会创建一个隔离浏览器会话的代理——`browser_navigate` 和 `browser_snapshot` 调用落入了不同的浏览器上下文（成功导航后 snapshot 返回 `about:blank`）。唯一可行的架构是 backend → 直接池实例，无代理层。

**规则**：如果使用了代理/挂载模式，第一个测试应该是：导航到一个页面，然后 snapshot，验证 snapshot 显示的是同一个页面。在这之前不要继续。

### 4. 截断进入 agent 状态的数据

Playwright MCP 的 `browser_snapshot` 返回 YAML 无障碍树。一个搜索结果页面产生 71KB 的 YAML。当存入 `execution_log`（LangGraph agent 状态）时，agent 调用 5 次 snapshot 就是 355KB 的状态——所有这些都会在节点间序列化并作为 LLM 提示词上下文发送。单次搜索任务的 token 使用量达到 206K。

**规则**：在 `call_tool` 边界处，将快照数据截断到仅 LLM 所需的内容（ref 行，最多 30 条）。绝不在不裁剪的情况下将原始外部服务输出存入 agent 状态。

### 5. 每个配置文件一个职责——禁止跨文件默认值

清理之前：`mcp_settings.json` 的端口是 8090，`.env` 中 `MCP_SERVER_PORT=8100`，`mcp_client.py` 有硬编码的 `localhost:8090/mcp` 回退值，`main.py` 有硬编码的 `8090` 默认值，`vite.config.ts` 在不同时期指向 `8000`/`8001`/`8002`。每个文件对使用哪个端口都有自己的理解。

**清理之后**：`mcp_settings.json` 是所有 MCP 客户端连接的唯一配置来源。`.env` 用于运维配置（LLM 密钥、浏览器设置、池大小）。任何 Python/TypeScript 文件中都不应有硬编码的 URL 或端口。

### 6. 不要通过改端口来解决问题——杀掉占用端口的进程

在调试过程中，端口被改了 6 次以上（8090→8100→8091→9400→8100...），而不是找出并杀掉占用端口的进程。在 Windows 上，通过 `cmd //c` 使用 `taskkill /F /PID <pid>` 在 bash 的 `taskkill` 不工作时有效。端口 8090 因为 Windows 系统进程将其用作临时源端口而永久不可用。

### 7. 浏览器可见性是配置问题，而非硬编码标志

Playwright MCP 池在 npx 参数中硬编码了 `--headless`，忽略了 `.env` 中的 `BROWSER_HEADLESS=false`。用户期望看到浏览器窗口但得到的是无头实例。始终通过池构造函数传递此类标志并检查环境变量。

### 8. MCP/浏览器变更的 E2E 冒烟测试清单

在声称任何 MCP 相关变更"完成"之前，运行以下测试：

```bash
# 1. 启动服务，发送一个简单任务
curl -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"task":"打开 bing.com 并搜索 test"}'

# 2. 从 SSE 流中验证：
#    - plan_generated 包含合理的步骤
#    - browser_navigate 完成（页面 URL 正确）
#    - browser_snapshot 返回 refs（>0）
#    - browser_type/browser_click 使用 ref 目标（而非 CSS 选择器或描述）
#    - step_end 事件中没有 "error" 状态

# 3. 检查后端日志：
#    - "Discovered 23 tools"（工具数量正确）
#    - "Snapshot: N refs" 且 N>0
#    - "Screenshot captured" 且字符数 >0
#    - 没有 "tool_error" 或 "error" 消息

# 4. 检查会话文件大小（<50KB，而非数百 KB）
ls -la browsepilot/data/sessions/ | tail -1
```
