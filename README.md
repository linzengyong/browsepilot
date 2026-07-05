# BrowsePilot — AI 驱动的浏览器自动化 Agent

通过自然语言操控真实浏览器的 AI 个人助理。你说"帮我搜一下 iPhone 15 最新价格"，它会打开浏览器、导航、搜索、提取结果、对比分析——全程自动完成。

## 架构概览

```
用户输入 → Frontend (Vue 3) → Backend (FastAPI + LangGraph) → MCP Server → Playwright 浏览器
                ↑  SSE 流式返回  │  7 节点 StateGraph          │  Streamable HTTP
                └────────────────┴─────────────────────────────┘
```

三个服务，自上而下：

| 服务 | 技术栈 | 端口 |
|------|--------|------|
| **Frontend** (`frontend-vue/`) | Vue 3 + Pinia + TypeScript + Element Plus + Tailwind CSS | 5173 |
| **Backend** (`backend/app/`) | FastAPI + LangGraph + LangChain + DeepSeek API | 8000 |
| **browser-mcp** (`browser_mcp/`) | FastMCP + Playwright 1.52+ | 9400 |

**Agent 执行流程**（7 节点 StateGraph）：

```
classify → pre_observe → plan → execute → reflect → replan → answer
(意图分类)  (预观测)    (规划)  (执行)    (反思)    (重规划)   (回答)
                      ↑                                    │
                      └──────── 条件路由循环 ──────────────┘
```

## 核心特性

- **自然语言操控浏览器**：搜索、点击、输入、截图，全程自动
- **智能规划与反思**：Agent 自动拆解任务、执行、反思、必要时重新规划
- **零 Token 工具决策**：关键词+正则评分分类器，命中的步骤跳过 LLM 调用
- **五重安全熔断**：连续失败/停滞/重规划上限/步骤上限/完工保护
- **HITL 人机协同**：登录页/验证码自动检测并暂停，用户操作完成后继续
- **Skill 领域知识系统**：渐进式披露，按需注入电商比价等垂直领域操作规范
- **会话持久化**：完整执行日志、Token 消耗明细、截图回放

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+
- DeepSeek API Key（或其他 OpenAI 兼容 API）

### 安装

```bash
# 克隆仓库
git clone https://github.com/wumingqingshang/browsepilot.git
cd browsepilot

# 安装 Python 依赖
uv sync

# 安装前端依赖
cd frontend-vue && npm install && cd ..

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY
```

### 启动

```bash
# 一键启动（Windows）
start.bat

# 一键启动（Linux/Mac）
chmod +x start.sh && ./start.sh

# 或手动启动三个终端：
.venv/Scripts/python -m browser_mcp.main          # MCP Server :9400
.venv/Scripts/python -m uvicorn backend.app.main:app --port 8000  # Backend :8000
cd frontend-vue && npm run dev                     # Frontend :5173
```

启动后打开 http://localhost:5173，在对话框中输入任务即可。

### 运行测试

```bash
.venv/Scripts/python -m pytest tests/ -v
```

## 配置说明

核心配置在 `.env` 中（参见 `.env.example`）：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `OPENAI_API_KEY` | LLM API Key | 必填 |
| `OPENAI_BASE_URL` | API 地址 | `https://api.deepseek.com/v1` |
| `BIG_MODEL` | 主 Agent 模型 | `deepseek-v4-flash` |
| `SMALL_MODEL` | 意图分类模型 | `deepseek-chat` |
| `BROWSER_HEADLESS` | 浏览器无头模式 | `true` |
| `ALLOWED_DOMAINS` | 域名白名单 | `github.com,baidu.com,wikipedia.org` |
| `USE_PLAYWRIGHT_MCP` | 使用 Playwright MCP 池 | `false`（自建模式） |

MCP Server 连接配置在 `mcp_settings.json` 中，是唯一的连接信息来源。

## 项目结构

```
browsepilot/
├── backend/app/          # FastAPI 后端 + LangGraph Agent
│   ├── agent/
│   │   ├── nodes.py      # 核心：7 个节点的实现（~1100 行）
│   │   ├── graph.py      # StateGraph 构图与路由
│   │   ├── prompts.py    # 各节点 Prompt 模板
│   │   ├── state.py      # AgentState 定义
│   │   └── skill_loader.py # Skill 渐进式加载系统
│   ├── main.py           # FastAPI 入口 + SSE 流式端点
│   ├── mcp_client.py     # MCP 客户端封装
│   ├── config.py         # 配置管理（含熔断参数）
│   ├── events.py         # SSE 事件类型定义
│   └── session_manager.py # 会话持久化
├── browser_mcp/          # MCP Server（双模式）
│   ├── tools/            # 8 个浏览器工具
│   └── main.py           # 入口（自建/Playwright 池切换）
├── frontend-vue/         # Vue 3 前端
│   └── src/
│       ├── stores/       # Pinia 状态管理
│       ├── components/   # Vue 组件
│       └── api/          # SSE 客户端
├── skills/               # Skill 领域知识库
├── tests/                # 测试
├── .env.example          # 环境变量模板
├── mcp_settings.json     # MCP 连接配置（唯一来源）
└── start.bat / start.sh  # 一键启动脚本
```

## 开发方式

本项目采用 SDD + TDD 双层驱动的 AI 协作方法论，Claude Code + DeepSeek API 交付。架构设计和核心算法由人工主导，AI 辅助编码实现。

更多技术细节见 [CLAUDE.md](./CLAUDE.md)。

## License

MIT
