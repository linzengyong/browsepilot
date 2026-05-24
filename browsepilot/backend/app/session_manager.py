"""Session lifecycle management and persistence to JSON files."""

import json
import os
import asyncio
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from loguru import logger

from backend.app.config import settings


class SessionManager:
    """Manages agent session lifecycle: create, update, persist, replay.

    Data model: a session contains multiple turns. Each turn has its own
    task, execution_log, final_answer, and token_usage. Session-level
    fields (custom_name, pinned) are preserved across persists.
    """

    def __init__(self, max_active_sessions: int = 10):
        self._active_sessions: dict[str, dict] = {}
        self._max_sessions = max_active_sessions
        os.makedirs(f"{settings.data_dir}/sessions", exist_ok=True)
        os.makedirs(f"{settings.data_dir}/screenshots", exist_ok=True)

    def create_session(self, session_id: str) -> dict:
        if len(self._active_sessions) >= self._max_sessions:
            raise HTTPException(
                status_code=429,
                detail=f"Too many active sessions (max {self._max_sessions})",
            )
        session = {
            "session_id": session_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "turns": [],
            "custom_name": "",
            "pinned": False,
        }
        self._active_sessions[session_id] = session
        return session

    def start_turn(self, session_id: str, task: str) -> dict:
        """Create a new turn for the session. Enforces max turns and max tokens."""
        session = self._active_sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        turns = session.get("turns", [])

        if len(turns) >= settings.max_turns_per_session:
            raise HTTPException(
                status_code=429,
                detail="max_turns_reached",
            )

        total_tokens = sum(
            t.get("token_usage", {}).get("prompt", 0)
            + t.get("token_usage", {}).get("completion", 0)
            for t in turns
        )
        if total_tokens >= settings.max_session_tokens:
            raise HTTPException(
                status_code=429,
                detail="max_session_tokens_reached",
            )

        turn = {
            "turn_index": len(turns),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "task": task,
            "execution_log": [],
            "final_answer": "",
            "token_usage": {},
        }
        turns.append(turn)
        return turn

    def update_current_turn(self, session_id: str, **kwargs) -> None:
        """Update fields on the latest turn. Only writes turn-level fields."""
        session = self._active_sessions.get(session_id)
        if not session:
            return
        turns = session.get("turns", [])
        if not turns:
            return
        turn_fields = {"execution_log", "final_answer", "token_usage", "status",
                        "pending_human_action", "human_prompt", "hitl_triggered_count"}
        turn_update = {k: v for k, v in kwargs.items() if k in turn_fields}
        turns[-1].update(turn_update)

    def update(self, session_id: str, **kwargs) -> None:
        """Legacy method — delegates to update_current_turn for turn-level fields."""
        self.update_current_turn(session_id, **kwargs)

    def append_log(self, session_id: str, entry: dict) -> None:
        """Append to current turn's execution_log."""
        session = self._active_sessions.get(session_id)
        if not session:
            return
        turns = session.get("turns", [])
        if turns:
            turns[-1].setdefault("execution_log", []).append(entry)

    def persist(self, session_id: str) -> str:
        """Persist session to JSON. Preserves custom_name and pinned."""
        session = self._active_sessions.get(session_id)
        if not session:
            return ""
        session["status"] = "completed"
        turns = session.get("turns", [])
        if turns:
            turns[-1]["status"] = "completed"
        filepath = f"{settings.data_dir}/sessions/{session_id}.json"
        data = {
            "session_id": session["session_id"],
            "created_at": session.get("created_at"),
            "status": session.get("status"),
            "turns": session.get("turns", []),
            "custom_name": session.get("custom_name", ""),
            "pinned": session.get("pinned", False),
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        logger.info("Session {} persisted to {}", session_id, filepath)
        return filepath

    def get_history(self, session_id: str) -> dict | None:
        """Return session data. Wraps old single-task format into turns array."""
        # Check in-memory first — avoids disk read for active sessions
        session = self._active_sessions.get(session_id)
        if session:
            return session
        filepath = f"{settings.data_dir}/sessions/{session_id}.json"
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Backward compat: old format without turns array
            if "turns" not in data:
                data["turns"] = [
                    {
                        "turn_index": 0,
                        "created_at": data.get("created_at", ""),
                        "status": data.get("status", "unknown"),
                        "task": data.get("task", ""),
                        "execution_log": data.get("execution_log", []),
                        "final_answer": data.get("final_answer", ""),
                        "token_usage": data.get("token_usage", {}),
                    }
                ]
            return data
        return None

    def get_replay(self, session_id: str, turn_index: int = -1) -> list[dict]:
        """Return replay steps for a specific turn. -1 = latest turn."""
        session = self.get_history(session_id)
        if not session:
            return []
        turns = session.get("turns", [])
        if not turns:
            return []
        if turn_index == -1:
            turn_index = len(turns) - 1
        if turn_index < 0 or turn_index >= len(turns):
            return []
        target_turn = turns[turn_index]
        return [
            {
                "step_index": i,
                "step": e.get("step", ""),
                "screenshot_path": e.get("screenshot_path", ""),
                "timestamp": e.get("timestamp", ""),
            }
            for i, e in enumerate(target_turn.get("execution_log", []))
        ]

    def list_sessions(self) -> list[dict]:
        """List sessions, pinned first, then by creation time descending."""
        sessions_dir = Path(f"{settings.data_dir}/sessions")
        if not sessions_dir.exists():
            return []
        results = []
        for f in sessions_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                session_id = data.get("session_id") or f.stem
                if not session_id or session_id == "None":
                    continue
                turns = data.get("turns", [])
                latest_turn = turns[-1] if turns else {}
                results.append(
                    {
                        "id": session_id,
                        "task_summary": (latest_turn.get("task", "")
                                         or data.get("task", "")
                                         or "")[:30],
                        "created_at": data.get("created_at", ""),
                        "status": data.get("status", "unknown"),
                        "custom_name": data.get("custom_name", ""),
                        "pinned": data.get("pinned", False),
                        "turn_count": len(turns),
                    }
                )
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
        results.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        results.sort(key=lambda r: 0 if r.get("pinned") else 1)
        return results

    def delete_session(self, session_id: str) -> bool:
        """Delete session persistent file. Returns True if successful."""
        filepath = Path(f"{settings.data_dir}/sessions/{session_id}.json")
        if filepath.exists():
            filepath.unlink()
            logger.info("Session {} deleted", session_id)
            return True
        return False

    def rename_session(self, session_id: str, name: str) -> bool:
        """Set custom_name on session and persist."""
        filepath = Path(f"{settings.data_dir}/sessions/{session_id}.json")
        if not filepath.exists():
            return False
        data = json.loads(filepath.read_text(encoding="utf-8"))
        data["custom_name"] = name
        filepath.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        if session_id in self._active_sessions:
            self._active_sessions[session_id]["custom_name"] = name
        return True

    def toggle_pin(self, session_id: str, pinned: bool) -> bool:
        """Set pinned flag on session and persist."""
        filepath = Path(f"{settings.data_dir}/sessions/{session_id}.json")
        if not filepath.exists():
            return False
        data = json.loads(filepath.read_text(encoding="utf-8"))
        data["pinned"] = pinned
        filepath.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        if session_id in self._active_sessions:
            self._active_sessions[session_id]["pinned"] = pinned
        return True

    def _delete_session_files(self, session_id: str):
        """Delete session JSON + all associated screenshots across all turns."""
        filepath = Path(f"{settings.data_dir}/sessions/{session_id}.json")
        if filepath.exists():
            try:
                data = json.loads(filepath.read_text(encoding="utf-8"))
                for turn in data.get("turns", []):
                    for entry in turn.get("execution_log", []):
                        screenshot_path = entry.get("screenshot_path", "")
                        if screenshot_path:
                            try:
                                Path(screenshot_path).unlink(missing_ok=True)
                            except OSError:
                                pass
            except (json.JSONDecodeError, OSError):
                pass
            filepath.unlink(missing_ok=True)

        screenshots_dir = Path(f"{settings.data_dir}/screenshots")
        session_screenshots = screenshots_dir / session_id
        if session_screenshots.exists():
            try:
                session_screenshots.rmdir()
            except OSError:
                pass

    async def schedule_cleanup(
        self, session_id: str, mcp_client=None, delay_minutes: int = None
    ) -> None:
        if delay_minutes is None:
            delay_minutes = settings.session_ttl_minutes
        await asyncio.sleep(delay_minutes * 60)

        self._delete_session_files(session_id)

        if session_id in self._active_sessions:
            del self._active_sessions[session_id]

        if mcp_client:
            await mcp_client.close()

        logger.info(
            "Session {} cleaned up after {} minutes", session_id, delay_minutes
        )

    def cleanup_on_startup(self):
        """Scan and delete sessions beyond count limit and orphan screenshots."""
        max_count = getattr(settings, "max_sessions_count", 100)
        sessions_dir = Path(f"{settings.data_dir}/sessions")
        if not sessions_dir.exists():
            return

        sessions = sorted(
            sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime
        )
        if len(sessions) > max_count:
            for old in sessions[: -max_count]:
                session_id = old.stem
                logger.info("Startup cleanup: removing old session {}", session_id)
                self._delete_session_files(session_id)

        self._cleanup_orphan_screenshots()

    def _cleanup_orphan_screenshots(self):
        """Delete screenshot dirs with no corresponding session JSON."""
        screenshots_dir = Path(f"{settings.data_dir}/screenshots")
        if not screenshots_dir.exists():
            return
        for child in screenshots_dir.iterdir():
            if child.is_dir():
                session_json = Path(
                    f"{settings.data_dir}/sessions/{child.name}.json"
                )
                if not session_json.exists():
                    shutil.rmtree(child, ignore_errors=True)
                    logger.info("Removed orphan screenshots: {}", child)

    def check_storage_before_write(self) -> bool:
        """Check data/ dir size, trigger emergency cleanup if over limit."""
        max_storage_mb = getattr(settings, "max_storage_mb", 500)
        total_size = self._get_data_dir_size()
        if total_size > max_storage_mb * 1024 * 1024:
            logger.warning(
                "Storage {} MB exceeds limit {} MB, emergency cleanup",
                total_size // (1024 * 1024),
                max_storage_mb,
            )
            self._emergency_cleanup(ratio=0.2)
            if self._get_data_dir_size() > max_storage_mb * 1024 * 1024:
                logger.warning("Storage still full after cleanup")
                return False
        return True

    def _get_data_dir_size(self) -> int:
        total = 0
        data_dir = Path(settings.data_dir)
        if not data_dir.exists():
            return 0
        for f in data_dir.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    def _emergency_cleanup(self, ratio: float = 0.2):
        """Delete the oldest ratio of sessions to free space."""
        sessions_dir = Path(f"{settings.data_dir}/sessions")
        if not sessions_dir.exists():
            return
        sessions = sorted(
            sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime
        )
        to_delete = int(len(sessions) * ratio)
        for old in sessions[:to_delete]:
            self._delete_session_files(old.stem)
