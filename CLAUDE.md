# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common Commands

```bash
# Run all tests
cd browsepilot && .venv/Scripts/python -m pytest tests/ -v

# Run backend only (import check)
.venv/Scripts/python -c "from backend.app.main import app; print('OK')"

# Start services (three terminals):
python -m browser_mcp.main          # MCP server :8090
uvicorn backend.app.main:app --port 8000  # Backend :8000
cd frontend-vue && npm run dev      # Frontend :5173
```

**Python must be Python 3.14** at `C:\Users\lzy\AppData\Local\Programs\Python\Python314\python.exe`. The Windows Apps alias (`python3`) has sandboxing issues.

**pip mirror**: both pip (aliyun) and uv (aliyun) are configured globally.

## Architecture

Three services, top-to-bottom:

**Frontend** (`frontend-vue/`) — Vue 3 + Pinia + TypeScript. SSE stream via `/chat/stream`. Stores: `chat.ts` (messages, tokens, phase), `session.ts` (sidebar list, rename/pin).

**Backend** (`backend/app/`) — FastAPI + LangGraph. The agent is a 6-node StateGraph: `classify → plan → execute → reflect → replan → answer`. `nodes.py` is the core (~1100 lines), `graph.py` defines edges and routing conditions.

**browser-mcp** (`browser_mcp/`) — Standalone MCP server (Streamable HTTP). BrowserPool manages Playwright Chromium instances. 8 tools in `tools/`.

### Agent State (`backend/app/agent/state.py`)

AgentState is a TypedDict with fields for messages, task, plan, execution_log, token_usage, and control fields (retry_count, need_replan, stop_reason, etc.). Scalar fields use default "replace" reducer; only `messages` uses `add_messages`.

### Key Flow: execute_node

1. Build tool descriptions + recent_context (page structure + tool call history)
2. **Scoring classifier** (`classify_tool`) — keyword (+1) + regex (+2) scoring across 8 tools, threshold=2
3. **Arg extraction** (`extract_args`) — regex-based extraction for navigate (URL), type_text (text), screenshot/get_content/get_page_structure (no args). Returns `(args_dict, is_complete)`.
4. Classifier hit + complete args → skip LLM entirely
5. Classifier hit + partial/no args → LLM fills remaining args. `REQUIRES_ARGS` check prevents empty args for click/type_text/navigate/execute_script. On failure, falls through to full LLM selection.
6. Classifier miss → full LLM tool+args selection

### Step Limits (dual-layer)

`plan_step_count` (reset to 0 on plan/replan, limit **15**) tracks steps within current plan. `total_steps` (never reset, limit **30**) is global. Both checked in `reflect_node` as OR conditions.

### Heuristic Checks (`_run_heuristic_checks`)

Zero-LLM-cost checks on last execution step: content_short (get_content only), domain_mutation, similar_results, few_elements, click_no_navigation (URL didn't change after nav-click), mcp_validation_error.

### Token Tracking

`accumulate_tokens()` in nodes.py records per-node breakdown. Answer streaming tokens are captured in main.py from `llm.astream()` — if `usage_metadata` unavailable on the last chunk, estimates are used and the frontend shows "(估算)".

### Session Persistence

`SessionManager` persists to `data/sessions/{id}.json`. `list_sessions` sorts pinned-first then by created_at. Fields persisted: execution_log, final_answer, token_usage, stop_reason, total_steps, plan_step_count.

## Session Analysis

```bash
# Quick overview (jq, auto-allowed)
jq -r '.execution_log | to_entries[] | "\(.key): \(.value.step[:50]) | \(.value.tool) | \(.value.result.status // "?")"' browsepilot/data/sessions/SESSION_ID.json

# Full detail (python, needs confirmation)
cd browsepilot && .venv/Scripts/python -c "
import json
with open('data/sessions/SESSION_ID.json', encoding='utf-8') as f:
    d = json.load(f)
for i, e in enumerate(d['execution_log']):
    print(f'{i}: {e[\"step\"][:50]} | {e[\"tool\"]} | {e[\"result\"].get(\"status\",\"?\")}')
"
```

## Tool Preference

Prefer built-in tools over `python -c "..."` for ad-hoc data exploration — built-in tools don't trigger permission prompts:

| Task | Use |
|------|-----|
| Read file contents | `Read` |
| Search code / text | `Grep` |
| Find files by name | `Glob` |
| Parse / filter JSON | `jq` (already auto-allowed) |
| Syntax check on edit | Auto-triggered by PostToolUse hook |
| Import / health check | Use the exact commands in Common Commands above |

`python -c` is still fine for complex multi-step data transforms, but 80% of one-liners are replaceable with Grep/Read/jq.

## Notes

- The `.claude/` directory is in `.gitignore`. Plan files in `docs/superpowers/` are not tracked by git.
- `browsepilot/.env` is gitignored but must exist for the backend to start (contains `OPENAI_API_KEY`).
- When debugging agent behavior, the session JSON in `data/sessions/` is more informative than `browsepilot.log` — it contains full execution_log, token breakdowns, and stop_reason.
- The `_route_reflect` function in `graph.py` should NOT contain step limit checks — those belong in `reflect_node` which sets stop_reason before routing.
- When receiving a request, first discuss the solution plan, rather than immediately starting to code.
- When debugging CSS layout issues (overflow, scrolling, element sizing), always trace the full parent grid/flex height chain from the root container through every intermediate wrapper before changing any single component. Check for auto-sized rows/columns that break height inheritance down the chain.
- Before pushing to GitHub, verify: (1) README.md is at repository root, (2) stale .md files and doc folders are cleaned, (3) .gitignore excludes data directories, environment files, and generated outputs.
