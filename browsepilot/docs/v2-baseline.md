# V2.0 基线记录（P0 Task 1）

- 分支：`v2.0`（自 master 创建，master HEAD = 64b84fe）
- 日期：2026-08-03
- 旧测试基线：`uv run pytest tests/ -q` → **66 passed, 1 warning in 3.62s**（无失败项）
  - warning：`backend/app/config.py:19` PydanticDeprecatedSince20（class-based config 弃用，非失败）
  - 注意：`uv sync` 默认不装 `[project.optional-dependencies]`，需 `uv sync --all-extras` 才能装上 pytest/pytest-asyncio
- 旧代码入口：backend/app/main.py（FastAPI :8000）、browser_mcp/main.py（MCP :9400）
- 旧应用导入验证：`from backend.app.main import app` → `old app ok: BrowsePilot API`
- 已知根因清单：见设计文档 §1.2（白名单卡死 / domcontentloaded 等待 / 选择器脆弱 / 无重试 / 熔断即放弃 / 上帝文件 / 状态无类型 / 硬编码 OpenAI 格式 / MCP 解析嗅探 / 结构化输出靠猜 / 范式问题）
- P0 红线：本阶段不修改 backend/app、browser_mcp、frontend-vue 业务代码

## 环境备注

- uv 0.10.4，pyproject `[[tool.uv.index]]` 清华镜像已配置
- `.env` 在 `.gitignore` 中，未入库（含真实 API key，不得提交）
- 本机 Python：pyproject 约束 `>=3.11`，uv 自动解析
