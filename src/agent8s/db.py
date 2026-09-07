from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    extra_write_dirs TEXT,
    needs_fresh_session INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_state (
    chat_id INTEGER PRIMARY KEY,
    current_project_id INTEGER REFERENCES projects(id),
    current_agent TEXT NOT NULL DEFAULT 'claude',
    active_task_id INTEGER REFERENCES tasks(id),
    parked_task_id INTEGER REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS sent_reminders (
    event_uid TEXT NOT NULL,
    event_start TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (event_uid, event_start)
);

CREATE TABLE IF NOT EXISTS yc_balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    balance REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS yc_balance_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL
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
    extra_write_dirs: list[str] = field(default_factory=list)
    needs_fresh_session: bool = False


@dataclass
class ChatState:
    chat_id: int
    current_project_id: Optional[int]
    current_agent: str
    active_task_id: Optional[int]
    parked_task_id: Optional[int]


class Database:
    def __init__(self, path: Path):
        self._path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        # CREATE TABLE IF NOT EXISTS doesn't add columns to a table that
        # already existed before this column was introduced.
        chat_state_columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_state)")}
        if "parked_task_id" not in chat_state_columns:
            conn.execute("ALTER TABLE chat_state ADD COLUMN parked_task_id INTEGER REFERENCES tasks(id)")

        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "extra_write_dirs" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN extra_write_dirs TEXT")
        if "needs_fresh_session" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN needs_fresh_session INTEGER NOT NULL DEFAULT 0")

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
                    "INSERT INTO chat_state (chat_id, current_project_id, current_agent, active_task_id, parked_task_id) "
                    "VALUES (?, NULL, 'claude', NULL, NULL)",
                    (chat_id,),
                )
                return ChatState(
                    chat_id=chat_id, current_project_id=None, current_agent="claude",
                    active_task_id=None, parked_task_id=None,
                )
            return ChatState(
                chat_id=r["chat_id"],
                current_project_id=r["current_project_id"],
                current_agent=r["current_agent"],
                active_task_id=r["active_task_id"],
                parked_task_id=r["parked_task_id"],
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

    def set_parked_task(self, chat_id: int, task_id: Optional[int]) -> None:
        self.get_chat_state(chat_id)
        with self._connect() as conn:
            conn.execute("UPDATE chat_state SET parked_task_id = ? WHERE chat_id = ?", (task_id, chat_id))

    # -- tasks --

    def create_task(
        self, project_id: int, chat_id: int, agent_name: str, branch: str, worktree_path: str, prompt: str
    ) -> Task:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO tasks
                   (project_id, chat_id, agent_name, branch, worktree_path, session_id, status, prompt,
                    extra_write_dirs, needs_fresh_session, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, NULL, 'running', ?, NULL, 0, ?, ?)""",
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

        If the task got far enough to have a session_id, claude/codex itself
        persisted that session to disk independent of our process — that's
        recoverable via /continue (status 'interrupted'). No session_id means
        there's nothing to resume ('failed').
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE status = 'running'").fetchall()
            stale = [self._row_to_task(r) for r in rows]
            for task in stale:
                new_status = "interrupted" if task.session_id else "failed"
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (new_status, now(), task.id)
                )
                task.status = new_status
                conn.execute(
                    "UPDATE chat_state SET active_task_id = NULL WHERE chat_id = ? AND active_task_id = ?",
                    (task.chat_id, task.id),
                )
                conn.execute(
                    "UPDATE chat_state SET parked_task_id = NULL WHERE chat_id = ? AND parked_task_id = ?",
                    (task.chat_id, task.id),
                )
            return stale

    def find_resumable_task(self, chat_id: int, exclude_task_id: Optional[int] = None) -> Optional[Task]:
        with self._connect() as conn:
            r = conn.execute(
                """SELECT * FROM tasks
                   WHERE chat_id = ? AND status IN ('active', 'interrupted') AND id != ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (chat_id, exclude_task_id or -1),
            ).fetchone()
            return self._row_to_task(r) if r else None

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

    def add_task_extra_dir(self, task_id: int, path: str) -> list[str]:
        """Grant an additional writable directory to a task, and mark it as
        needing a fresh agent session — codex specifically can't widen an
        existing session's sandbox via resume(), so the next turn has to
        start() a new session in the same worktree instead (see agents/base.py)."""
        task = self.get_task(task_id)
        dirs = task.extra_write_dirs if task else []
        if path not in dirs:
            dirs = [*dirs, path]
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET extra_write_dirs = ?, needs_fresh_session = 1, updated_at = ? WHERE id = ?",
                (json.dumps(dirs), now(), task_id),
            )
        return dirs

    def clear_task_needs_fresh_session(self, task_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE tasks SET needs_fresh_session = 0, updated_at = ? WHERE id = ?", (now(), task_id))

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

    # -- yandex cloud balance --

    def record_balance_snapshot(self, balance: float) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO yc_balance_snapshots (checked_at, balance) VALUES (?, ?)", (now(), balance))

    def get_balance_snapshot_near(self, target_hours_ago: float, tolerance_hours: float = 6) -> Optional[float]:
        """Closest recorded balance to `target_hours_ago` hours before now,
        within `tolerance_hours` — used to approximate spend over a period
        without a real per-service consumption API."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=target_hours_ago + tolerance_hours)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT checked_at, balance FROM yc_balance_snapshots WHERE checked_at >= ? ORDER BY checked_at ASC",
                (cutoff,),
            ).fetchall()
        if not rows:
            return None
        target_time = datetime.now(timezone.utc) - timedelta(hours=target_hours_ago)
        best = min(rows, key=lambda r: abs((datetime.fromisoformat(r["checked_at"]) - target_time).total_seconds()))
        if abs((datetime.fromisoformat(best["checked_at"]) - target_time).total_seconds()) > tolerance_hours * 3600:
            return None
        return best["balance"]

    def should_send_balance_alert(self, cooldown_hours: int) -> bool:
        with self._connect() as conn:
            r = conn.execute("SELECT sent_at FROM yc_balance_alerts ORDER BY sent_at DESC LIMIT 1").fetchone()
        if r is None:
            return True
        return datetime.now(timezone.utc) - datetime.fromisoformat(r["sent_at"]) >= timedelta(hours=cooldown_hours)

    def mark_balance_alert_sent(self) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO yc_balance_alerts (sent_at) VALUES (?)", (now(),))

    @staticmethod
    def _row_to_task(r: sqlite3.Row) -> Task:
        raw_dirs = r["extra_write_dirs"] if "extra_write_dirs" in r.keys() else None
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
            extra_write_dirs=json.loads(raw_dirs) if raw_dirs else [],
            needs_fresh_session=bool(r["needs_fresh_session"]) if "needs_fresh_session" in r.keys() else False,
        )
