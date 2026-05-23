"""FastAPI application entry point with SSE streaming task execution."""

import uuid
import json
import asyncio
import base64
import re
import time
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse
from loguru import logger
import sys

from backend.app.config import settings, get_mcp_server_config
from backend.app.mcp_client import MCPClient
from backend.app.agent.graph import build_graph
from backend.app.agent.state import AgentState
from backend.app.events import SSEData
from backend.app.session_manager import SessionManager

session_manager = SessionManager(max_active_sessions=settings.max_active_sessions)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    logger.add(f"{settings.data_dir}/browsepilot.log", rotation="10 MB", level="DEBUG")
    logger.info("BrowsePilot backend starting (model={})", settings.big_model)
    session_manager.cleanup_on_startup()
    yield
    logger.info("BrowsePilot backend shutting down")


app = FastAPI(title="BrowsePilot API", version="0.1.0", lifespan=lifespan)

screenshots_dir = Path(settings.data_dir) / "screenshots"
screenshots_dir.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=str(screenshots_dir)), name="screenshots")


def filter_user_input(text: str) -> str:
    """Filter user input: truncate long text, remove control characters."""
    if not isinstance(text, str):
        return ""
    text = text[:2000]
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    return text.strip()


@app.post("/chat/stream")
async def chat_stream(request: Request):
    body = await request.json()
    task = body.get("task", "")
    session_id = body.get("session_id") or str(uuid.uuid4())[:8]

    task = filter_user_input(task)
    if not task:
        raise HTTPException(status_code=400, detail="task is required")

    # Session lifecycle: create new or start a new turn in existing session
    history = session_manager.get_history(session_id)
    if not history:
        session_manager.create_session(session_id)
        turn = session_manager.start_turn(session_id, task)
        logger.info("New session {} turn {} with task: {}", session_id, turn["turn_index"], task[:80])
    else:
        turns = history.get("turns", [])
        last_turn = turns[-1] if turns else {}
        last_turn_completed = bool(last_turn.get("final_answer", ""))
        if task != last_turn.get("task", "") or last_turn_completed:
            turn = session_manager.start_turn(session_id, task)
            logger.info("Session {} new turn {} with task: {}", session_id, turn["turn_index"], task[:80])
        else:
            turn = last_turn
            logger.info("Session {} reusing turn {} (same task, incomplete): {}", session_id, turn.get("turn_index", 0), task[:80])

    mcp_client = MCPClient(get_mcp_server_config("browser-mcp"))

    async def event_generator():
        async def _cleanup_session():
            """Persist partial state and close MCP on exception."""
            try:
                if accumulated_state:
                    session_manager.update_current_turn(session_id,
                        execution_log=accumulated_state.get("execution_log", []),
                        final_answer=accumulated_state.get("final_answer", ""),
                        token_usage=accumulated_state.get("token_usage", {}),
                        status="failed",
                    )
                    session_manager.persist(session_id)
            except Exception:
                pass
            try:
                await mcp_client.close()
            except Exception:
                pass

        try:
            graph = build_graph(mcp_client, lazy_mcp=True)
            # MCP will be connected lazily when classify routes to browser_task

            # If resuming an existing session, restore its full state
            if history:
                logger.info("Resuming existing session {}", session_id)
                # Determine if we're reusing an incomplete turn or starting fresh
                if task == last_turn.get("task", "") and not last_turn_completed:
                    use_execution_log = last_turn.get("execution_log", [])
                    use_final_answer = last_turn.get("final_answer", "")
                    use_token_usage = last_turn.get("token_usage", {})
                    use_total_steps = len(use_execution_log)
                else:
                    use_execution_log = []
                    use_final_answer = ""
                    use_token_usage = {"prompt": 0, "completion": 0, "breakdown": {}}
                    use_total_steps = 0
                initial_state: AgentState = {
                    "messages": history.get("messages", []),
                    "task": task,
                    "session_id": session_id,
                    "intent": history.get("intent", "browser_task"),
                    "plan": history.get("plan", []),
                    "execution_log": use_execution_log,
                    "degradation_log": history.get("degradation_log", []),
                    "retry_count": history.get("retry_count", 0),
                    "need_replan": False,
                    "final_answer": use_final_answer,
                    "total_steps": use_total_steps,
                    "token_usage": use_token_usage,
                    "consecutive_failures": history.get("consecutive_failures", 0),
                    "stagnation_count": history.get("stagnation_count", 0),
                    "replan_count": history.get("replan_count", 0),
                    "stagnation_warning": False,
                    "completion_check_count": history.get("completion_check_count", 0),
                    "plan_step_count": 0,
                    "page_structure": history.get("page_structure", {}),
                    "page_screenshot": history.get("page_screenshot", ""),
                    "turn_index": turn.get("turn_index", 0) if turn else 0,
                    "session_turns": [
                        {"turn_index": t.get("turn_index", 0), "task": t.get("task", ""), "final_answer": t.get("final_answer", "")}
                        for t in history.get("turns", [])
                    ],
                }
            else:
                initial_state: AgentState = {
                    "messages": [],
                    "task": task,
                    "session_id": session_id,
                    "intent": "",
                    "plan": [],
                    "execution_log": [],
                    "degradation_log": [],
                    "retry_count": 0,
                    "need_replan": False,
                    "final_answer": "",
                    "total_steps": 0,
                    "plan_step_count": 0,
                    "page_structure": {},
                    "page_screenshot": "",
                    "token_usage": {"prompt": 0, "completion": 0, "breakdown": {}},
                    "consecutive_failures": 0,
                    "stagnation_count": 0,
                    "replan_count": 0,
                    "stagnation_warning": False,
                    "completion_check_count": 0,
                    "turn_index": turn.get("turn_index", 0) if turn else 0,
                    "session_turns": [],
                }

            graph_config = {
                "recursion_limit": 100,
                "configurable": {"thread_id": session_id},
            }

            # For resumed sessions, append the new user message
            if history:
                from langchain_core.messages import HumanMessage
                initial_state["messages"].append(HumanMessage(content=task))

            accumulated_state = dict(initial_state)
            yield SSEData.session_created(session_id, initial_state.get("turn_index", 0))

            # Stream with session timeout via asyncio.wait_for on each __anext__()
            timeout = getattr(settings, 'session_timeout_seconds', 300)
            deadline = time.monotonic() + timeout
            astream = graph.astream(initial_state, graph_config)

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("Session {} timed out after {}s", session_id, timeout)
                    yield SSEData.error("Session timed out. Partial results are shown below.")
                    break
                try:
                    event = await asyncio.wait_for(astream.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.warning("Session {} timed out during stream wait", session_id)
                    yield SSEData.error("Session timed out. Partial results are shown below.")
                    break

                for node_name, node_output in event.items():
                    accumulated_state.update(node_output)
                    if node_name == "classify":
                        yield SSEData.thinking_status("classifying", "正在分析用户意图...")
                        intent = node_output.get("intent", "unknown")

                    elif node_name == "plan":
                        yield SSEData.thinking_status("planning", "正在分析任务并制定执行计划...")
                        steps = node_output.get("plan", [])
                        tokens = node_output.get("token_usage", {})
                        yield SSEData.plan_generated(steps, tokens)

                    elif node_name == "execute":
                        if node_output.get("execution_log"):
                            last_log = node_output["execution_log"][-1]
                            step_index = len(node_output["execution_log"]) - 1
                            yield SSEData.thinking_status(
                                "executing",
                                f"正在执行: {last_log['step']}",
                                step_index + 1,
                                0,
                            )
                            yield SSEData.step_start(last_log["step"], step_index)
                            result = last_log.get("result", {})
                            screenshot_b64 = ""
                            if isinstance(result, dict):
                                screenshot_path = result.get("screenshot_path", "")
                                if screenshot_path and os.path.exists(screenshot_path):
                                    try:
                                        with open(screenshot_path, "rb") as f:
                                            screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
                                    except Exception:
                                        pass
                                elif result.get("screenshot_base64"):
                                    screenshot_b64 = result["screenshot_base64"]
                            if screenshot_b64:
                                yield SSEData.screenshot(
                                    screenshot_b64,
                                    last_log.get("timestamp", ""),
                                )
                            yield SSEData.step_end(last_log["step"], result)

                    elif node_name == "reflect":
                        yield SSEData.thinking_status("reflecting", "正在反思执行结果...")
                        decision = "replan" if node_output.get("need_replan") else "success"
                        yield SSEData.reflection(decision, "")

                    elif node_name == "replan":
                        yield SSEData.thinking_status("replanning", "正在重新规划替代方案...")
                        yield SSEData.replan(node_output.get("plan", []))

                    elif node_name == "answer":
                        yield SSEData.thinking_status("answering", "正在生成最终回答...")
                        answer_messages = node_output.get("answer_messages", [])
                        if answer_messages:
                            from backend.app.agent.nodes import get_llm
                            llm = get_llm()
                            full_text = ""
                            last_chunk = None
                            async for chunk in llm.astream(answer_messages):
                                last_chunk = chunk
                                if hasattr(chunk, "content") and chunk.content:
                                    full_text += chunk.content
                                    yield SSEData.answer_chunk(chunk.content)
                            # Collect answer token usage
                            answer_usage = None
                            token_estimated = True
                            if last_chunk and hasattr(last_chunk, "usage_metadata"):
                                um = last_chunk.usage_metadata
                                if isinstance(um, dict) and um.get("input_tokens"):
                                    answer_usage = um
                                    token_estimated = False
                            if answer_usage is None:
                                from backend.app.agent.nodes import estimate_tokens
                                # Estimate: answer_messages prompt + completion
                                est_prompt = sum(estimate_tokens(str(m.content)) for m in answer_messages if hasattr(m, "content"))
                                answer_usage = {"input_tokens": est_prompt, "output_tokens": estimate_tokens(full_text)}
                            # Accumulate into state
                            if accumulated_state:
                                from backend.app.agent.nodes import accumulate_tokens
                                accumulated_state["token_usage"] = accumulate_tokens(
                                    accumulated_state.get("token_usage", {}), answer_usage, "answer"
                                )
                                accumulated_state["final_answer"] = full_text
                                if token_estimated:
                                    accumulated_state["token_usage"]["estimated"] = True
                                # Send token update with estimated flag
                                tu = accumulated_state["token_usage"]
                                yield SSEData.token_update(
                                    tu.get("prompt", 0), tu.get("completion", 0),
                                    estimated=token_estimated,
                                )
                            yield SSEData.final_answer(full_text, estimate_tokens(full_text))
                        else:
                            yield SSEData.final_answer("", 0)

                    # Token updates from any node
                    if node_output.get("token_usage"):
                        tu = node_output["token_usage"]
                        yield SSEData.token_update(
                            tu.get("prompt", 0), tu.get("completion", 0)
                        )

            # Persist session using accumulated state
            if accumulated_state:
                session_manager.update_current_turn(session_id,
                    execution_log=accumulated_state.get("execution_log", []),
                    final_answer=accumulated_state.get("final_answer", ""),
                    token_usage=accumulated_state.get("token_usage", {}),
                    status="completed",
                )
            session_manager.persist(session_id)

            # Close MCP connection promptly (don't wait for TTL)
            await mcp_client.close()

            # Schedule session cleanup (disk cleanup only)
            asyncio.create_task(
                session_manager.schedule_cleanup(session_id)
            )

        except RuntimeError as e:
            if "cancel scope" in str(e):
                logger.debug("Session {} anyio cancel scope cleanup: {}", session_id, e)
            else:
                logger.exception("Runtime error in session {}", session_id)
                yield SSEData.error(str(e))
            await _cleanup_session()
        except Exception as e:
            logger.exception("Error in session {}", session_id)
            yield SSEData.error(str(e))
            await _cleanup_session()

    async def sse_formatted_generator():
        async for event in event_generator():
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse_formatted_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/history/{session_id}")
async def get_history(session_id: str):
    data = session_manager.get_history(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(content=data)


@app.get("/replay/{session_id}")
async def get_replay(session_id: str, turn_index: int = -1):
    session = session_manager.get_history(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    turns = session.get("turns", [])
    if turn_index == -1:
        turn_index = len(turns) - 1 if turns else 0
    target_turn = turns[turn_index] if 0 <= turn_index < len(turns) else {}
    steps = []
    for i, e in enumerate(target_turn.get("execution_log", [])):
        step_data = {
            "step_index": i,
            "step": e.get("step", ""),
            "screenshot_path": e.get("screenshot_path", ""),
            "timestamp": e.get("timestamp", ""),
            "result": (e.get("result", {}) if isinstance(e.get("result"), dict) else {}),
        }
        steps.append(step_data)
    return JSONResponse(content=steps)


@app.get("/sessions")
async def list_sessions():
    return JSONResponse(content=session_manager.list_sessions())


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    ok = session_manager.delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(content={"ok": True})


@app.patch("/sessions/{session_id}/rename")
async def rename_session(session_id: str, request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    ok = session_manager.rename_session(session_id, name)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(content={"ok": True})


@app.patch("/sessions/{session_id}/pin")
async def toggle_pin(session_id: str, request: Request):
    body = await request.json()
    pinned = body.get("pinned", False)
    ok = session_manager.toggle_pin(session_id, bool(pinned))
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(content={"ok": True})


@app.get("/health")
async def health():
    return {"status": "ok"}
