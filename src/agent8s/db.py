from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    chat_id INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    branch TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    session_id TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    prompt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_state (
    chat_id INTEGER PRIMARY KEY,
    current_project_id INTEGER REFERENCES projects(id),
    current_agent TEXT NOT NULL DEFAULT 'claude',
    active_task_id INTEGER REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS sent_reminders (
    event_uid TEXT NOT NULL,
    event_start TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (event_uid, event_start)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Project:
    id: int
    name: str
    path: str
    default_branch: str


@dataclass
class Task:
    id: int
    project_id: int
    chat_id: int
    agent_name: str
    branch: str
    worktree_path: str
    session_id: Optional[str]
    status: str
    prompt: str


@dataclass
class ChatState:
    chat_id: int
    current_project_id: Optional[int]
    current_agent: str
    active_task_id: Optional[int]


class Database:
    def __init__(self, path: Path):
        self._path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- projects --

    def add_project(self, name: str, path: str, default_branch: str) -> Project:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO projects (name, path, default_branch, created_at) VALUES (?, ?, ?, ?)",
                (name, path, default_branch, now()),
            )
            return Project(id=cur.lastrowid, name=name, path=path, default_branch=default_branch)

    def list_projects(self) -> list[Project]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
            return [Project(id=r["id"], name=r["name"], path=r["path"], default_branch=r["default_branch"]) for r in rows]

    def get_project_by_name(self, name: str) -> Optional[Project]:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
            return Project(id=r["id"], name=r["name"], path=r["path"], default_branch=r["default_branch"]) if r else None

    def get_project(self, project_id: int) -> Optional[Project]:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return Project(id=r["id"], name=r["name"], path=r["path"], default_branch=r["default_branch"]) if r else None

    # -- chat state --

    def get_chat_state(self, chat_id: int) -> ChatState:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
            if r is None:
                conn.execute(
                    "INSERT INTO chat_state (chat_id, current_project_id, current_agent, active_task_id) VALUES (?, NULL, 'claude', NULL)",
                    (chat_id,),
                )
                return ChatState(chat_id=chat_id, current_project_id=None, current_agent="claude", active_task_id=None)
            return ChatState(
                chat_id=r["chat_id"],
                current_project_id=r["current_project_id"],
                current_agent=r["current_agent"],
                active_task_id=r["active_task_id"],
            )

    def set_current_project(self, chat_id: int, project_id: int) -> None:
        self.get_chat_state(chat_id)
        with self._connect() as conn:
            conn.execute("UPDATE chat_state SET current_project_id = ? WHERE chat_id = ?", (project_id, chat_id))

    def set_current_agent(self, chat_id: int, agent_name: str) -> None:
        self.get_chat_state(chat_id)
        with self._connect() as conn:
            conn.execute("UPDATE chat_state SET current_agent = ? WHERE chat_id = ?", (agent_name, chat_id))

    def set_active_task(self, chat_id: int, task_id: Optional[int]) -> None:
        self.get_chat_state(chat_id)
        with self._connect() as conn:
            conn.execute("UPDATE chat_state SET active_task_id = ? WHERE chat_id = ?", (task_id, chat_id))

    # -- tasks --

    def create_task(
        self, project_id: int, chat_id: int, agent_name: str, branch: str, worktree_path: str, prompt: str
    ) -> Task:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO tasks
                   (project_id, chat_id, agent_name, branch, worktree_path, session_id, status, prompt, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, NULL, 'running', ?, ?, ?)""",
                (project_id, chat_id, agent_name, branch, worktree_path, prompt, now(), now()),
            )
            return Task(
                id=cur.lastrowid,
                project_id=project_id,
                chat_id=chat_id,
                agent_name=agent_name,
                branch=branch,
                worktree_path=worktree_path,
                session_id=None,
                status="running",
                prompt=prompt,
            )

    def reconcile_stale_running_tasks(self) -> list[Task]:
        """Call once at startup. A task can only be 'running' during the
        lifetime of the process that started its subprocess — any task still
        marked 'running' when a fresh process starts belongs to a run that
        never got to report back (crash, force-kill, unhandled exception),
        and would otherwise sit stuck forever with the chat blocked on it.
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE status = 'running'").fetchall()
            stale = [self._row_to_task(r) for r in rows]
            for task in stale:
                conn.execute(
                    "UPDATE tasks SET status = 'failed', updated_at = ? WHERE id = ?", (now(), task.id)
                )
                conn.execute(
                    "UPDATE chat_state SET active_task_id = NULL WHERE chat_id = ? AND active_task_id = ?",
                    (task.chat_id, task.id),
                )
            return stale

    def set_task_branch_and_worktree(self, task_id: int, branch: str, worktree_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET branch = ?, worktree_path = ?, updated_at = ? WHERE id = ?",
                (branch, worktree_path, now(), task_id),
            )

    def get_task(self, task_id: int) -> Optional[Task]:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._row_to_task(r) if r else None

    def update_task_status(self, task_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, now(), task_id))

    def update_task_session(self, task_id: int, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE tasks SET session_id = ?, updated_at = ? WHERE id = ?", (session_id, now(), task_id))

    # -- reminders --

    def was_reminded(self, event_uid: str, event_start: str) -> bool:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT 1 FROM sent_reminders WHERE event_uid = ? AND event_start = ?", (event_uid, event_start)
            ).fetchone()
            return r is not None

    def mark_reminded(self, event_uid: str, event_start: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sent_reminders (event_uid, event_start, sent_at) VALUES (?, ?, ?)",
                (event_uid, event_start, now()),
            )

    @staticmethod
    def _row_to_task(r: sqlite3.Row) -> Task:
        return Task(
            id=r["id"],
            project_id=r["project_id"],
            chat_id=r["chat_id"],
            agent_name=r["agent_name"],
            branch=r["branch"],
            worktree_path=r["worktree_path"],
            session_id=r["session_id"],
            status=r["status"],
            prompt=r["prompt"],
        )
