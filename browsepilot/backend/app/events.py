"""SSE event type definitions for real-time streaming to frontend."""

import json
from typing import Any


class SSEData:
    """Helper to format SSE event dictionaries."""

    @staticmethod
    def plan_generated(steps: list, token_usage: dict) -> dict:
        return {"event": "plan_generated", "data": {"steps": steps, "token_usage": token_usage}}

    @staticmethod
    def step_start(step: str, step_index: int) -> dict:
        return {"event": "step_start", "data": {"step": step, "step_index": step_index}}

    @staticmethod
    def screenshot(base64_data: str, timestamp: str) -> dict:
        return {"event": "screenshot", "data": {"base64": base64_data, "timestamp": timestamp}}

    @staticmethod
    def step_end(step: str, result: dict) -> dict:
        return {"event": "step_end", "data": {"step": step, "result": result}}

    @staticmethod
    def reflection(decision: str, reason: str) -> dict:
        return {"event": "reflection", "data": {"decision": decision, "reason": reason}}

    @staticmethod
    def replan(new_steps: list) -> dict:
        return {"event": "replan", "data": {"new_steps": new_steps}}

    @staticmethod
    def token_update(prompt: int, completion: int, estimated: bool = False) -> dict:
        return {"event": "token_update", "data": {"prompt": prompt, "completion": completion, "estimated": estimated}}

    @staticmethod
    def answer_chunk(content: str) -> dict:
        return {"event": "answer_chunk", "data": {"content": content}}

    @staticmethod
    def final_answer(content: str, total_tokens: int) -> dict:
        return {"event": "final_answer", "data": {"content": content, "total_tokens": total_tokens}}

    @staticmethod
    def thinking_status(phase: str, message: str, step_index: int = 0, total_steps: int = 0) -> dict:
        return {"event": "thinking_status", "data": {"phase": phase, "message": message, "step_index": step_index, "total_steps": total_steps}}

    @staticmethod
    def error(message: str) -> dict:
        return {"event": "error", "data": {"message": message}}

    @staticmethod
    def session_created(session_id: str, turn_index: int = 0) -> dict:
        return {"event": "session_created", "data": {"session_id": session_id, "turn_index": turn_index}}

    @staticmethod
    def turn_started(turn_index: int) -> dict:
        return {"event": "turn_started", "data": {"turn_index": turn_index}}

    @staticmethod
    def human_intervention_needed(prompt: str) -> dict:
        """SSE event: agent needs human help (login, captcha, etc.)."""
        return {"event": "human_intervention_needed", "data": {"prompt": prompt}}
