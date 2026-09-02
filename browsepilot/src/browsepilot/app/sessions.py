"""Session metadata store (SQLite, stdlib sqlite3 — one file, no ORM)."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self._conn.commit()

    def create_session(self, thread_id: str) -> str:
        sid = str(uuid.uuid4())[:8]
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
