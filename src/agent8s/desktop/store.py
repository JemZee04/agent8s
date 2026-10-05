from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from ..db import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'worktree',
    branch TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    agent TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT '',
    sessions TEXT NOT NULL DEFAULT '{}',
    extra_dirs TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'idle',
    changes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    agent TEXT,
    model TEXT,
    parts TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
"""

_CHAT_FIELDS = {"title", "agent", "model", "effort", "status", "changes", "branch", "worktree_path"}


@dataclass
class Chat:
    id: int
    project_id: int
    title: str
    mode: str
    branch: str
    worktree_path: str
    agent: str
    model: str
    effort: str
    status: str
    changes: int
    created_at: str
    updated_at: str
    # Per agent: {"id": native session id, "seen": id of the last message
    # that session has actually seen}. Drives what must be re-told to an
    # agent when it picks the conversation back up after another agent
    # handled some turns.
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    extra_dirs: list[str] = field(default_factory=list)


@dataclass
class Message:
    id: int
    chat_id: int
    role: str
    agent: Optional[str]
    model: Optional[str]
    parts: list[dict[str, Any]]
    status: str
    created_at: str


class Store:
    def __init__(self, path: Path):
        self._path = path
        with self._connect() as conn:
            # WAL lets the Telegram bot and the desktop app share this file
            # without readers blocking writers.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- chats --

    def create_chat(
        self,
        project_id: int,
        title: str,
        mode: str,
        branch: str,
        worktree_path: str,
        agent: str,
        model: str = "",
        effort: str = "",
        created_at: Optional[str] = None,
    ) -> Chat:
        stamp = created_at or now()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO chats (project_id, title, mode, branch, worktree_path, agent, model, effort,
                                      created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, title, mode, branch, worktree_path, agent, model, effort, stamp, stamp),
            )
            chat_id = cur.lastrowid
        return self.get_chat(chat_id)  # type: ignore[return-value]

    def get_chat(self, chat_id: int) -> Optional[Chat]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return _row_to_chat(row) if row else None

    def list_chats(self) -> list[Chat]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM chats ORDER BY updated_at DESC, id DESC").fetchall()
        return [_row_to_chat(r) for r in rows]

    def update_chat(self, chat_id: int, **fields: Any) -> None:
        unknown = set(fields) - _CHAT_FIELDS
        if unknown:
            raise ValueError(f"cannot update chat fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE chats SET {assignments}, updated_at = ? WHERE id = ?",
                (*fields.values(), now(), chat_id),
            )

    def set_session(self, chat_id: int, agent: str, session_id: str, seen: int) -> None:
        chat = self.get_chat(chat_id)
        if chat is None:
            return
        sessions = {**chat.sessions, agent: {"id": session_id, "seen": seen}}
        with self._connect() as conn:
            conn.execute("UPDATE chats SET sessions = ? WHERE id = ?", (json.dumps(sessions), chat_id))

    def clear_session(self, chat_id: int, agent: str) -> None:
        chat = self.get_chat(chat_id)
        if chat is None or agent not in chat.sessions:
            return
        sessions = {k: v for k, v in chat.sessions.items() if k != agent}
        with self._connect() as conn:
            conn.execute("UPDATE chats SET sessions = ? WHERE id = ?", (json.dumps(sessions), chat_id))

    def add_extra_dir(self, chat_id: int, path: str) -> list[str]:
        chat = self.get_chat(chat_id)
        dirs = chat.extra_dirs if chat else []
        if path not in dirs:
            dirs = [*dirs, path]
        with self._connect() as conn:
            conn.execute("UPDATE chats SET extra_dirs = ? WHERE id = ?", (json.dumps(dirs), chat_id))
        return dirs

    def delete_chat(self, chat_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    # -- messages --

    def add_message(
        self,
        chat_id: int,
        role: str,
        parts: list[dict[str, Any]],
        agent: Optional[str] = None,
        model: Optional[str] = None,
        status: str = "done",
        created_at: Optional[str] = None,
    ) -> Message:
        stamp = created_at or now()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO messages (chat_id, role, agent, model, parts, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, role, agent, model, json.dumps(parts), status, stamp),
            )
            message_id = cur.lastrowid
            conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (stamp, chat_id))
        return self.get_message(message_id)  # type: ignore[return-value]

    def get_message(self, message_id: int) -> Optional[Message]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _row_to_message(row) if row else None

    def update_message(
        self, message_id: int, parts: Optional[list[dict[str, Any]]] = None, status: Optional[str] = None
    ) -> None:
        with self._connect() as conn:
            if parts is not None:
                conn.execute("UPDATE messages SET parts = ? WHERE id = ?", (json.dumps(parts), message_id))
            if status is not None:
                conn.execute("UPDATE messages SET status = ? WHERE id = ?", (status, message_id))

    def list_messages(self, chat_id: int, limit: Optional[int] = None, before: Optional[int] = None) -> tuple[list[Message], bool]:
        """Messages in ascending order. With `limit`, the newest `limit` messages older than
        `before`; the flag says whether older ones remain."""
        with self._connect() as conn:
            if limit is None:
                rows = conn.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
                return [_row_to_message(r) for r in rows], False
            rows = conn.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                (chat_id, before if before is not None else 2**62, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        return [_row_to_message(r) for r in reversed(rows[:limit])], more

    def add_messages_bulk(self, chat_id: int, rows: list[tuple[str, Optional[str], Optional[str], list[dict[str, Any]], str, Optional[str]]]) -> int:
        """Insert (role, agent, model, parts, status, created_at) rows in one transaction; returns the last id."""
        stamp_now = now()
        last = 0
        with self._connect() as conn:
            for role, agent, model, parts, status, created_at in rows:
                cur = conn.execute(
                    """INSERT INTO messages (chat_id, role, agent, model, parts, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (chat_id, role, agent, model, json.dumps(parts), status, created_at or stamp_now),
                )
                last = cur.lastrowid
            if rows:
                conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (rows[-1][5] or stamp_now, chat_id))
        return last

    def messages_between(self, chat_id: int, after_id: int, before_id: int) -> list[Message]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND id > ? AND id < ? ORDER BY id",
                (chat_id, after_id, before_id),
            ).fetchall()
        return [_row_to_message(r) for r in rows]

    # -- recovery --

    def reconcile_interrupted(self) -> int:
        """A turn can't survive a process restart; don't leave it 'running' forever."""
        with self._connect() as conn:
            conn.execute("UPDATE messages SET status = 'interrupted' WHERE status = 'running'")
            cur = conn.execute("UPDATE chats SET status = 'idle' WHERE status = 'running'")
            return cur.rowcount


def _row_to_chat(r: sqlite3.Row) -> Chat:
    return Chat(
        id=r["id"],
        project_id=r["project_id"],
        title=r["title"],
        mode=r["mode"],
        branch=r["branch"],
        worktree_path=r["worktree_path"],
        agent=r["agent"],
        model=r["model"],
        effort=r["effort"],
        status=r["status"],
        changes=r["changes"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        sessions=json.loads(r["sessions"] or "{}"),
        extra_dirs=json.loads(r["extra_dirs"] or "[]"),
    )


def _row_to_message(r: sqlite3.Row) -> Message:
    return Message(
        id=r["id"],
        chat_id=r["chat_id"],
        role=r["role"],
        agent=r["agent"],
        model=r["model"],
        parts=json.loads(r["parts"]),
        status=r["status"],
        created_at=r["created_at"],
    )
