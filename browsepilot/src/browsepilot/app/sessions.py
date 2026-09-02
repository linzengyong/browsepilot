"""Session metadata store (SQLite, stdlib sqlite3 — one file, no ORM)."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionInfo(BaseModel):
    id: str
    thread_id: str
    name: str
    pinned: bool = False
    created_at: str = ""
    updated_at: str = ""


class SessionStore:
    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                pinned INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                task TEXT NOT NULL, answer TEXT NOT NULL, created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def create_session(self, thread_id: str, session_id: str | None = None) -> str:
        """Create a session row and return its id.

        session_id is None → 自生成短随机 id（历史调用方行为不变）；
        显式传入 → 行 id 使用给定值（chat/stream 需要行 id == 对外公布的
        effective_session_id，/sessions 与 /history 才能寻址同一会话）。
        """
        sid = session_id or str(uuid.uuid4())[:8]
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, thread_id, name, pinned, created_at, updated_at) VALUES (?,?,?,0,?,?)",
                (sid, thread_id, "", now, now),
            )
            self._conn.commit()
        return sid

    def _row_to_info(self, row) -> SessionInfo:
        return SessionInfo(id=row[0], thread_id=row[1], name=row[2], pinned=bool(row[3]),
                           created_at=row[4], updated_at=row[5])

    def get_session(self, session_id: str) -> SessionInfo | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return self._row_to_info(row) if row else None

    def record_turn(self, session_id: str, task: str, answer: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO turns (session_id, task, answer, created_at) VALUES (?,?,?,?)",
                (session_id, task, answer, _now()),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?", (_now(), session_id)
            )
            self._conn.commit()

    def list_turns(self, session_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT task, answer, created_at FROM turns WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [{"task": r[0], "answer": r[1], "created_at": r[2]} for r in rows]

    def list_sessions(self) -> list[SessionInfo]:
        rows = self._conn.execute("SELECT * FROM sessions ORDER BY pinned DESC, updated_at DESC").fetchall()
        return [self._row_to_info(r) for r in rows]

    def rename_session(self, session_id: str, name: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE sessions SET name=?, updated_at=? WHERE id=?", (name, _now(), session_id))
            self._conn.commit()

    def toggle_pin(self, session_id: str) -> bool:
        with self._lock:
            self._conn.execute("UPDATE sessions SET pinned=1-pinned, updated_at=? WHERE id=?", (_now(), session_id))
            self._conn.commit()
            row = self._conn.execute("SELECT pinned FROM sessions WHERE id=?", (session_id,)).fetchone()
        return bool(row[0]) if row else False

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self._conn.commit()
