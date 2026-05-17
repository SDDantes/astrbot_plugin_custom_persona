from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class ConversationLedger:
    def __init__(self, db_path: Path, *, per_chat_limit: int = 1000) -> None:
        self.db_path = db_path
        self.per_chat_limit = per_chat_limit
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    user_id TEXT,
                    user_name TEXT,
                    content_json TEXT NOT NULL,
                    ts REAL NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_id, ts)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session_del "
                "ON messages(session_id, deleted)"
            )
            self._conn.commit()

    def record(
        self,
        *,
        session_id: str,
        role: str,
        content: list[dict[str, Any]] | str,
        user_id: str = "",
        user_name: str = "",
        ts: float | None = None,
    ) -> None:
        payload = content if isinstance(content, list) else [{"type": "text", "text": content}]
        now = time.time() if ts is None else ts
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO messages(session_id, role, user_id, user_name, content_json, ts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    user_id,
                    user_name,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
            self._prune_session_locked(session_id)
            self._conn.commit()

    def recent(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT role, user_id, user_name, content_json, ts
                FROM messages
                WHERE session_id = ? AND deleted = 0
                ORDER BY ts DESC
                LIMIT ?
                """,
                (session_id, max(1, int(limit))),
            ).fetchall()
        result = []
        for role, user_id, user_name, content_json, ts in reversed(rows):
            try:
                content = json.loads(content_json)
            except json.JSONDecodeError:
                content = [{"type": "text", "text": content_json}]
            result.append(
                {
                    "role": role,
                    "user_id": user_id,
                    "user_name": user_name,
                    "content": content,
                    "timestamp": ts,
                }
            )
        return result

    def soft_delete_session(self, session_id: str) -> int:
        """Mark all messages for *session_id* as deleted. Returns count affected."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET deleted = 1 WHERE session_id = ? AND deleted = 0",
                (session_id,),
            )
            self._conn.commit()
            return cur.rowcount

    def _prune_session_locked(self, session_id: str) -> None:
        self._conn.execute(
            """
            DELETE FROM messages
            WHERE session_id = ?
              AND id NOT IN (
                SELECT id FROM messages
                WHERE session_id = ? AND deleted = 0
                ORDER BY ts DESC
                LIMIT ?
              )
            """,
            (session_id, session_id, self.per_chat_limit),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
