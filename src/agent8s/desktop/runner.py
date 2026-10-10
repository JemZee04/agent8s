from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .. import git_ops
from ..config import DesktopConfig
from ..db import Database, Project
from .agent_auth import explain_error
from .catalog import build_catalog
from .drivers import ClaudeDriver, CodexDriver, TurnRequest
from . import importer
from .handoff import build_turn_prompt
from .store import Chat, Message, Store

log = logging.getLogger("agent8s.desktop")

DEFAULT_TITLE = "Новый чат"
DIFF_LIMIT = 2_000_000
FLUSH_INTERVAL = 1.0
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}$")  # never starts with "-": it would read as a CLI flag
_MISSING_SESSION = re.compile(r"(no conversation found|session.*not found|thread.*not found|no rollout)", re.I)


TEMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")


class Busy(Exception):
    """The chat already has a turn in flight."""


class UserError(Exception):
    """A request that can't be honoured; the message is safe to show as-is."""


# -- event reducer ----------------------------------------------------------


def apply_event(parts: list[dict[str, Any]], event: dict[str, Any]) -> list[dict[str, Any]]:
    """Fold one normalized agent event into a message's parts.

    Returns UI ops describing exactly what changed, so the client mirrors the
    state by applying ops instead of re-implementing this logic:
      {"op": "append", "i": index, "text": str}   # extend a text/thinking part
      {"op": "set",    "i": index, "part": dict}  # create or replace a part
    """
    kind = event.get("type")
    if kind in ("text", "thinking"):
        if parts and parts[-1]["type"] == kind:
            parts[-1]["text"] += event["text"]
            return [{"op": "append", "i": len(parts) - 1, "text": event["text"]}]
        parts.append({"type": kind, "text": event["text"]})
        return [{"op": "set", "i": len(parts) - 1, "part": parts[-1]}]
    if kind == "tool_start":
        parts.append(
            {
                "type": "tool", "id": event["id"], "name": event["name"],
                "input": None, "output": None, "is_error": False, "status": "running",
            }
        )
        return [{"op": "set", "i": len(parts) - 1, "part": parts[-1]}]
    if kind in ("tool_input", "tool_end"):
        for i in range(len(parts) - 1, -1, -1):
            part = parts[i]
            if part["type"] == "tool" and part["id"] == event["id"]:
                if kind == "tool_input":
                    part["input"] = event["input"]
                else:
                    part.update(output=event["output"], is_error=event["is_error"], status="done")
                return [{"op": "set", "i": i, "part": part}]
        return []
    if kind == "usage":
        usage = {k: v for k, v in event.items() if k != "type" and v is not None}
        if not usage:
            return []
        parts.append({"type": "usage", **usage})
        return [{"op": "set", "i": len(parts) - 1, "part": parts[-1]}]
    if kind == "done" and not event.get("ok"):
        parts.append({"type": "error", "text": event.get("error") or "Ход завершился с ошибкой"})
        return [{"op": "set", "i": len(parts) - 1, "part": parts[-1]}]
    return []


def _without_turns_made_here(new: list[importer.ParsedMessage], made_here: list[str]) -> list[importer.ParsedMessage]:
    """Drop file turns whose prompt was typed in this app (they are already in the chat)."""
    pending = list(made_here)
    kept: list[importer.ParsedMessage] = []
    skipping = False
    for message in new:
        if message.role == "user":
            text = message.parts[0].get("text")
            skipping = text in pending
            if skipping:
                pending.remove(text)
                continue
        elif skipping:
            continue
        kept.append(message)
    return kept


# -- pub/sub ------------------------------------------------------------------


class Hub:
    """Fan-out of server events to connected UI clients.

    Each client gets a bounded queue; one that can't keep up is dropped (it
    reconnects and resyncs from REST) instead of stalling every agent turn.
    """

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def publish(self, payload: dict[str, Any]) -> None:
        for queue in list(self._queues):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._queues.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)  # sentinel: tells the connection to close and resync


# -- orchestration --------------------------------------------------------


@dataclass
class LiveTurn:
    message: dict[str, Any]
    started_at: float
    task: Optional[asyncio.Task] = None
    rev: int = 0


def message_to_dict(m: Message) -> dict[str, Any]:
    return asdict(m)


class Orchestrator:
    def __init__(self, config: DesktopConfig, db: Database, store: Store, hub: Hub):
        self._config = config
        self._db = db
        self._store = store
        self._hub = hub
        self._live: dict[int, LiveTurn] = {}
        self._session_files: dict[str, Path] = {}
        self._drivers = {
            "claude": ClaudeDriver(config.claude_allowed_tools, config.claude_permission_mode, config.turn_timeout_seconds),
            "codex": CodexDriver(config.codex_sandbox, config.turn_timeout_seconds),
        }
        self.catalog = build_catalog()

    # -- queries --

    def chat_to_dict(self, chat: Chat) -> dict[str, Any]:
        project = self._db.get_project(chat.project_id)
        live = self._live.get(chat.id)
        data = asdict(chat)
        data.update(
            claude_stale=self.claude_stale(chat),
            project_name=project.name if project else "?",
            running=live is not None,
            running_since=live.started_at if live else None,
            default_branch=project.default_branch if project else "",
            sessions=sorted(chat.sessions),
        )
        return data

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        return self.chat_to_dict(self._require_chat(chat_id))

    # -- keeping imported chats in step with the Claude Code session file --

    def _claude_file(self, session_id: str) -> Optional[Path]:
        path = self._session_files.get(session_id)
        if path is None or not path.exists():
            path = importer.find_session_file(session_id)
            if path is None:
                return None
            self._session_files[session_id] = path
        return path

    def claude_stale(self, chat: Chat) -> bool:
        """Has the Claude session moved on (e.g. you kept working in the terminal) since this chat last looked?"""
        session = chat.sessions.get("claude")
        if not session:
            return False
        path = self._claude_file(session["id"])
        if path is None:
            return False
        if session.get("offset") is None:
            return True  # an old chat that also has turns made here: one careful refresh sorts it out
        try:
            return path.stat().st_size > session["offset"]
        except OSError:
            return False

    async def sync_claude(self, chat_id: int) -> dict[str, Any]:
        """Bring the chat up to date with its Claude Code session file; returns {"added": n, "pending": bool}."""
        chat = self._require_chat(chat_id)
        if chat_id in self._live:
            raise Busy()
        session = chat.sessions.get("claude")
        if not session:
            raise UserError("Этот чат не связан с сессией Claude Code.")
        path = self._claude_file(session["id"])
        if path is None:
            raise UserError("Файл сессии Claude Code не найден (возможно, он удалён).")
        return await self._sync_claude_file(chat, session, path)

    async def _sync_claude_file(self, chat: Chat, session: dict[str, Any], path: Path) -> dict[str, Any]:
        history, _ = self._store.list_messages(chat.id)
        last_before = history[-1].id if history else 0
        # Chats imported before offsets were recorded: the imported messages are those carrying a file
        # timestamp ("...Z"); messages made in this app are stamped by us ("...+00:00").
        offset = session.get("offset")
        made_here: list[str] = []
        if offset is None:
            imported = sum(1 for m in history if m.created_at.endswith("Z"))
            offset = await asyncio.to_thread(importer.legacy_offset, path, imported)
            made_here = [m.parts[0]["text"] for m in history
                         if m.role == "user" and not m.created_at.endswith("Z") and m.parts and m.parts[0].get("text")]
        parsed = await asyncio.to_thread(importer.parse_session, path, offset)
        new = _without_turns_made_here(parsed.messages, made_here)

        last_id = last_before
        if new:
            last_id = self._store.add_messages_bulk(chat.id, [
                (m.role, "claude" if m.role == "assistant" else None, m.model, m.parts, m.status, m.created_at) for m in new
            ])
        # Claude itself wrote these turns, so it has seen them -- unless another agent spoke in between,
        # in which case it must still be told about that (hence `seen` only moves when nothing is missed).
        fully_seen = session.get("seen") == last_before
        seen = last_id if (new and fully_seen) else session.get("seen", last_before)
        self._store.set_session(chat.id, "claude", session["id"], seen=seen, offset=parsed.end_offset)
        if parsed.new_title and parsed.new_title != chat.title:
            self._store.update_chat(chat.id, title=parsed.new_title[:120])
        fresh = self._require_chat(chat.id)
        self._hub.publish({"t": "chat", "chat": self.chat_to_dict(fresh)})
        if new:
            self._hub.publish({"t": "chat_reload", "chat_id": chat.id})
        return {"added": len(new), "pending": parsed.pending}

    async def _autosync_before_turn(self, chat: Chat) -> None:
        """A turn made here resumes the session: what happened in it meanwhile must already be in this chat."""
        session = chat.sessions.get("claude")
        if chat.agent != "claude" or not session or not self.claude_stale(chat):
            return
        try:
            path = self._claude_file(session["id"])
            if path is not None:
                await self._sync_claude_file(chat, session, path)
        except Exception:
            log.exception("could not sync chat %s with its Claude session before a turn", chat.id)

    def _mark_claude_session_read(self, chat_id: int) -> None:
        """After a turn here, the session file also holds that turn: it is not "new" from outside."""
        chat = self._store.get_chat(chat_id)
        session = chat.sessions.get("claude") if chat else None
        path = self._claude_file(session["id"]) if session else None
        if path is not None:
            self._store.set_session(chat_id, "claude", session["id"], seen=session["seen"], offset=importer.end_of_complete_lines(path))

    def calibrate_claude_offsets(self) -> int:
        """One-time: give chats imported before offsets existed their offset (no messages change)."""
        done = 0
        for chat in self._store.list_chats():
            session = chat.sessions.get("claude")
            if not session or session.get("offset") is not None:
                continue
            path = self._claude_file(session["id"])
            if path is None:
                continue
            history, _ = self._store.list_messages(chat.id)
            if any(m.role == "user" and not m.created_at.endswith("Z") for m in history):
                continue  # turns made here are in the file too: only a refresh can tell them apart from news
            imported = sum(1 for m in history if m.created_at.endswith("Z"))
            self._store.set_session(chat.id, "claude", session["id"], seen=session["seen"],
                                    offset=importer.legacy_offset(path, imported))
            done += 1
        return done

    def known_folders(self) -> list[str]:
        """Every project and chat folder: the places a shared HTML file may reach up to."""
        return [p.path for p in self._db.list_projects()] + [c.worktree_path for c in self._store.list_chats() if c.worktree_path]

    def other_folders_inside(self, chat_id: int) -> list[str]:
        """Folders of other chats/projects that sit inside this chat's folder."""
        chat = self._require_chat(chat_id)
        mine = Path(chat.worktree_path).resolve()
        inside = []
        for folder in self.known_folders():
            candidate = Path(folder).resolve()
            if candidate != mine and mine in candidate.parents and str(candidate) not in inside:
                inside.append(str(candidate))
        return inside

    def list_chats(self) -> list[dict[str, Any]]:
        return [self.chat_to_dict(c) for c in self._store.list_chats()]

    def get_chat_with_messages(self, chat_id: int, limit: Optional[int] = None, before: Optional[int] = None) -> dict[str, Any]:
        chat = self._require_chat(chat_id)
        page, more = self._store.list_messages(chat_id, limit, before)
        messages = [message_to_dict(m) for m in page]
        live = self._live.get(chat_id)
        if live and before is None:
            # The in-memory message is ahead of the database (text deltas are
            # only flushed periodically); rev lets the client drop stale ops.
            messages = [m for m in messages if m["id"] != live.message["id"]]
            messages.append({**live.message, "rev": live.rev})
        return {"chat": self.chat_to_dict(chat), "messages": messages, "has_more": more}

    # -- projects --

    def add_project(self, name: str, path: str, branch: Optional[str] = None) -> Project:
        name = name.strip()
        if not name:
            raise UserError("Нужно имя проекта.")
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            raise UserError("Нужен абсолютный путь.")
        check = git_ops.check_repo(resolved)
        if not check.ok:
            raise UserError(f"{resolved}: {check.reason}")
        if self._db.get_project_by_name(name):
            raise UserError(f"Проект «{name}» уже существует.")
        return self._db.add_project(name, str(resolved.resolve()), branch or git_ops.default_branch(resolved))

    # -- chats --

    async def create_chat(
        self, project_id: int, agent: str, model: str = "", effort: str = "", mode: str = "worktree"
    ) -> dict[str, Any]:
        project = self._db.get_project(project_id)
        if project is None:
            raise UserError("Проект не найден.")
        self._validate_agent(agent, model, effort)
        if mode not in ("worktree", "direct"):
            raise UserError("mode: worktree или direct.")

        project_path = Path(project.path)
        if mode == "direct":
            try:
                branch = await asyncio.to_thread(git_ops.current_branch, project_path)
            except git_ops.GitError:
                branch = ""  # a plain folder (notes, study material): works in place, just without git tools
            chat = self._store.create_chat(project_id, DEFAULT_TITLE, mode, branch, str(project_path), agent, model, effort)
        else:
            if not project.default_branch:
                raise UserError(
                    f"«{project.name}» — не git-репозиторий, отдельную ветку для неё создать нельзя. "
                    "Выберите режим «Прямо в проекте»."
                )
            chat = self._store.create_chat(project_id, DEFAULT_TITLE, mode, "", "", agent, model, effort)
            branch = f"agent8s/chat-{chat.id}"
            worktree = self._config.worktree_dir / project.name / f"chat-{chat.id}"
            try:
                await asyncio.to_thread(git_ops.create_worktree, project_path, branch, worktree, project.default_branch)
            except git_ops.GitError as exc:
                self._store.delete_chat(chat.id)
                raise UserError(str(exc)) from exc
            self._store.update_chat(chat.id, branch=branch, worktree_path=str(worktree))
        chat_dict = self.chat_to_dict(self._require_chat(chat.id))
        self._hub.publish({"t": "chat", "chat": chat_dict})
        return chat_dict

    def update_chat(self, chat_id: int, title: Optional[str], agent: Optional[str], model: Optional[str], effort: Optional[str]) -> dict[str, Any]:
        chat = self._require_chat(chat_id)
        fields: dict[str, Any] = {}
        if title is not None and title.strip():
            fields["title"] = title.strip()[:120]

        switching = any(v is not None for v in (agent, model, effort))
        if switching:
            if chat_id in self._live:
                raise Busy()
            new_agent = agent if agent is not None else chat.agent
            new_model = model if model is not None else chat.model
            new_effort = effort if effort is not None else chat.effort
            # A model/effort that belonged to the previous agent is meaningless
            # for the new one.
            if agent is not None and agent != chat.agent:
                new_model = model if model is not None else ""
                new_effort = effort if effort is not None else ""
            self._validate_agent(new_agent, new_model, new_effort)
            if (new_agent, new_model, new_effort) != (chat.agent, chat.model, chat.effort):
                fields.update(agent=new_agent, model=new_model, effort=new_effort)
                label = self._label(new_agent, new_model)
                note = self._store.add_message(chat_id, "system", [{"type": "text", "text": f"Переключено на {label}"}])
                self._hub.publish({"t": "msg_new", "chat_id": chat_id, "message": message_to_dict(note)})

        if fields:
            self._store.update_chat(chat_id, **fields)
        chat_dict = self.chat_to_dict(self._require_chat(chat_id))
        self._hub.publish({"t": "chat", "chat": chat_dict})
        return chat_dict

    def add_extra_dir(self, chat_id: int, path: str) -> dict[str, Any]:
        self._require_chat(chat_id)
        target = Path(path).expanduser()
        if not target.is_absolute() or not target.is_dir():
            raise UserError("Нужен абсолютный путь к существующей папке.")
        self._store.add_extra_dir(chat_id, str(target.resolve()))
        chat_dict = self.chat_to_dict(self._require_chat(chat_id))
        self._hub.publish({"t": "chat", "chat": chat_dict})
        return chat_dict

    async def discard(self, chat_id: int) -> None:
        chat = self._require_chat(chat_id)
        if chat_id in self._live:
            raise Busy()
        if chat.mode == "worktree":
            project = self._db.get_project(chat.project_id)
            if project and chat.worktree_path:
                await asyncio.to_thread(git_ops.remove_worktree, Path(project.path), Path(chat.worktree_path), chat.branch)
        self._store.delete_chat(chat_id)
        self._hub.publish({"t": "chat_deleted", "chat_id": chat_id})

    # -- import from Claude Code --

    def claude_sessions(self) -> list[dict[str, Any]]:
        imported = {c.sessions["claude"]["id"]: c.id for c in self._store.list_chats() if "claude" in c.sessions}
        rows = []
        for s in importer.list_sessions():
            reason = ""
            if s.id in imported:
                reason = "уже в agent8s"
            elif not s.cwd or not Path(s.cwd).is_dir():
                reason = "папка сессии больше не существует"
            elif s.cwd.startswith(TEMP_PREFIXES):
                reason = "временная папка (скорее всего, служебный запуск)"
            rows.append({
                "id": s.id, "title": s.title, "cwd": s.cwd, "branch": s.branch, "mtime": s.mtime,
                "size": s.size, "first_prompt": s.first_prompt,
                "imported": s.id in imported, "chat_id": imported.get(s.id), "importable": not reason, "reason": reason,
            })
        return rows

    async def import_claude(self, session_ids: list[str]) -> list[dict[str, Any]]:
        known = {s.id: s for s in await asyncio.to_thread(importer.list_sessions)}
        results = []
        for session_id in session_ids:
            info = known.get(session_id)
            if info is None:
                results.append({"id": session_id, "ok": False, "error": "Сессия не найдена."})
                continue
            try:
                chat = await self._import_session(info)
                results.append({"id": session_id, "ok": True, "chat_id": chat.id})
            except UserError as exc:
                results.append({"id": session_id, "ok": False, "error": str(exc)})
        return results

    async def _import_session(self, info: importer.SessionInfo) -> Chat:
        if any(c.sessions.get("claude", {}).get("id") == info.id for c in self._store.list_chats()):
            raise UserError("Эта сессия уже импортирована.")
        parsed = await asyncio.to_thread(importer.parse_session, info.path)
        if not parsed.messages:
            raise UserError("В сессии нет сообщений.")
        cwd = Path(parsed.cwd or info.cwd)
        if not cwd.is_dir():
            raise UserError(f"Папка сессии не найдена: {cwd}")
        try:
            root = await asyncio.to_thread(git_ops.toplevel, cwd)
            branch = await asyncio.to_thread(git_ops.current_branch, cwd)
            default_branch = await asyncio.to_thread(git_ops.default_branch, root)
        except git_ops.GitError:
            # Not a repository (notes, study folders, ...): the chat still works in place;
            # only the diff/commit/merge tools have nothing to act on.
            root, branch, default_branch = cwd, "", ""

        project = next((p for p in self._db.list_projects() if Path(p.path).resolve() == root.resolve()), None)
        if project is None:
            name, n = root.name or "project", 2
            while self._db.get_project_by_name(name):
                name, n = f"{root.name}-{n}", n + 1
            project = self._db.add_project(name, str(root), default_branch)

        # `claude --resume` finds a session only from the directory it started in, so the chat
        # works in place (direct mode) in exactly that directory.
        chat = self._store.create_chat(project.id, parsed.title, "direct", branch, str(cwd), "claude", created_at=parsed.first_at)
        last_id = self._store.add_messages_bulk(chat.id, [
            (m.role, "claude" if m.role == "assistant" else None, m.model, m.parts, m.status, m.created_at)
            for m in parsed.messages
        ])
        self._store.set_session(chat.id, "claude", info.id, seen=last_id, offset=parsed.end_offset)
        fresh = self._require_chat(chat.id)
        self._hub.publish({"t": "chat", "chat": self.chat_to_dict(fresh)})
        return fresh

    # -- git actions --

    async def diff(self, chat_id: int) -> dict[str, Any]:
        chat = self._require_chat(chat_id)
        path = Path(chat.worktree_path)
        try:
            text = await asyncio.to_thread(git_ops.diff_full, path)
            files = await asyncio.to_thread(git_ops.status_short, path)
        except git_ops.GitError as exc:  # e.g. index.lock held by the agent's own git call
            raise UserError(str(exc)) from exc
        count = len([line for line in files.splitlines() if line.strip()])
        if count != chat.changes:
            self._store.update_chat(chat_id, changes=count)
            self._hub.publish({"t": "chat", "chat": self.chat_to_dict(self._require_chat(chat_id))})
        truncated = len(text) > DIFF_LIMIT
        return {"diff": text[:DIFF_LIMIT], "truncated": truncated, "status": files}

    async def commit(self, chat_id: int, message: str) -> None:
        chat = self._require_chat(chat_id)
        if chat_id in self._live:
            raise Busy()
        path = Path(chat.worktree_path)
        if not await asyncio.to_thread(git_ops.has_changes, path):
            raise UserError("Нечего коммитить.")
        text = message.strip() or f"agent8s: {chat.title}"
        try:
            await asyncio.to_thread(git_ops.commit_all, path, text)
        except git_ops.GitError as exc:
            raise UserError(str(exc)) from exc
        await self._refresh_changes(chat_id)

    async def merge(self, chat_id: int, message: str) -> str:
        chat = self._require_chat(chat_id)
        if chat_id in self._live:
            raise Busy()
        if chat.mode != "worktree":
            raise UserError("Слияние нужно только для чатов в отдельной ветке; этот чат работает прямо в проекте.")
        project = self._db.get_project(chat.project_id)
        if project is None:
            raise UserError("Проект не найден.")
        path, project_path = Path(chat.worktree_path), Path(project.path)
        text = message.strip() or f"agent8s: {chat.title}"
        try:
            if await asyncio.to_thread(git_ops.has_changes, path):
                await asyncio.to_thread(git_ops.commit_all, path, text)
            await asyncio.to_thread(git_ops.merge_branch, project_path, chat.branch, text)
        except git_ops.GitError as exc:
            await asyncio.to_thread(git_ops.abort_merge, project_path)
            raise UserError(str(exc)) from exc
        await self._refresh_changes(chat_id)
        target = await asyncio.to_thread(git_ops.current_branch, project_path)
        return target

    # -- turns --

    async def send(self, chat_id: int, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise UserError("Пустое сообщение.")
        chat = self._require_chat(chat_id)
        if chat_id in self._live:
            raise Busy()
        if not Path(chat.worktree_path).is_dir():
            raise UserError(f"Папка чата не найдена: {chat.worktree_path}")

        await self._autosync_before_turn(chat)
        chat = self._require_chat(chat_id)
        user_msg = self._store.add_message(chat_id, "user", [{"type": "text", "text": text}])
        assistant_msg = self._store.add_message(
            chat_id, "assistant", [], agent=chat.agent, model=chat.model or None, status="running"
        )
        fields: dict[str, Any] = {"status": "running"}
        if chat.title == DEFAULT_TITLE:
            fields["title"] = " ".join(text.split())[:60]
        self._store.update_chat(chat_id, **fields)

        live = LiveTurn(message=message_to_dict(assistant_msg), started_at=time.time())
        live.task = asyncio.create_task(self._run_turn(chat_id, user_msg, assistant_msg, live))
        self._live[chat_id] = live

        self._hub.publish({"t": "msg_new", "chat_id": chat_id, "message": message_to_dict(user_msg)})
        self._hub.publish({"t": "msg_new", "chat_id": chat_id, "message": {**live.message, "rev": 0}})
        self._hub.publish({"t": "chat", "chat": self.chat_to_dict(self._require_chat(chat_id))})
        return {"user": message_to_dict(user_msg), "assistant": {**live.message, "rev": 0}}

    async def stop(self, chat_id: int) -> bool:
        live = self._live.get(chat_id)
        if live is None:
            return False
        live.task.cancel()
        await asyncio.gather(live.task, return_exceptions=True)
        return True

    async def shutdown(self) -> None:
        for chat_id in list(self._live):
            await self.stop(chat_id)

    async def _run_turn(self, chat_id: int, user_msg: Message, assistant_msg: Message, live: LiveTurn) -> None:
        chat = self._require_chat(chat_id)
        agent = chat.agent
        parts: list[dict[str, Any]] = live.message["parts"]
        status, ok_chat_status = "done", "idle"
        last_flush = time.monotonic()
        user_text = user_msg.parts[0]["text"]

        def emit(event: dict[str, Any]) -> None:
            nonlocal last_flush
            ops = apply_event(parts, event)
            if not ops:
                return
            live.rev += 1
            self._hub.publish({"t": "msg_ops", "chat_id": chat_id, "msg_id": assistant_msg.id, "rev": live.rev, "ops": ops})
            structural = any(op["op"] == "set" for op in ops)
            if structural or time.monotonic() - last_flush > FLUSH_INTERVAL:
                self._store.update_message(assistant_msg.id, parts=parts)
                last_flush = time.monotonic()

        try:
            driver = self._drivers[agent]
            done: Optional[dict[str, Any]] = None
            for attempt in (0, 1):
                chat = self._require_chat(chat_id)
                session = chat.sessions.get(agent)
                after = session["seen"] if session else 0
                unseen = self._store.messages_between(chat_id, after, user_msg.id)
                note = await asyncio.to_thread(self._workspace_note, Path(chat.worktree_path)) if unseen else ""
                request = TurnRequest(
                    prompt=build_turn_prompt(user_text, unseen, fresh=session is None, workspace_note=note),
                    cwd=Path(chat.worktree_path),
                    model=chat.model,
                    effort=chat.effort,
                    session_id=session["id"] if session else None,
                    extra_dirs=chat.extra_dirs,
                )
                done = None
                async for event in driver.run(request):
                    if event["type"] == "done":
                        done = event  # held back: a stale-session failure is retried silently
                        continue
                    if event["type"] == "session":
                        # The session has seen this prompt and will see its own reply.
                        self._store.set_session(chat_id, agent, event["id"], seen=assistant_msg.id)
                    emit(event)
                stale = (
                    attempt == 0
                    and request.session_id is not None
                    and not parts
                    and done is not None
                    and not done.get("ok")
                    and _MISSING_SESSION.search(done.get("error") or "")
                )
                if not stale:
                    break
                self._store.clear_session(chat_id, agent)  # retry once from scratch, with the transcript
            if done is None:
                done = {"type": "done", "ok": False, "error": "Агент завершился без результата"}
            if not done.get("ok"):
                done = {**done, "error": explain_error(agent, done.get("error"))}
            emit(done)
            if not done.get("ok"):
                status, ok_chat_status = "error", "error"
        except asyncio.CancelledError:
            status = "interrupted"
        except Exception as exc:  # never leave the chat stuck on "running"
            log.exception("turn failed in chat %s", chat_id)
            emit({"type": "done", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            status, ok_chat_status = "error", "error"
        finally:
            try:
                self._store.update_message(assistant_msg.id, parts=parts, status=status)
                self._store.update_chat(chat_id, status=ok_chat_status)
                live.message["status"] = status
                self._live.pop(chat_id, None)
                if agent == "claude":
                    self._mark_claude_session_read(chat_id)
                self._hub.publish({"t": "msg_status", "chat_id": chat_id, "msg_id": assistant_msg.id, "status": status})
                await self._refresh_changes(chat_id)
            except Exception:
                log.exception("failed to finalize turn in chat %s", chat_id)
                self._live.pop(chat_id, None)

    # -- helpers --

    async def _refresh_changes(self, chat_id: int) -> None:
        chat = self._store.get_chat(chat_id)
        if chat is None:
            return
        try:
            out = await asyncio.to_thread(git_ops.status_short, Path(chat.worktree_path))
        except Exception:
            out = ""
        count = len([line for line in out.splitlines() if line.strip()])
        self._store.update_chat(chat_id, changes=count)
        self._hub.publish({"t": "chat", "chat": self.chat_to_dict(self._store.get_chat(chat_id))})
        self._hub.publish({"t": "diff_changed", "chat_id": chat_id})

    @staticmethod
    def _workspace_note(path: Path) -> str:
        try:
            status = git_ops.status_short(path)
        except git_ops.GitError:
            return ""
        lines = status.splitlines()
        if not lines:
            return "(чисто — незакоммиченных изменений нет)"
        shown = "\n".join(lines[:30])
        return shown + (f"\n… и ещё {len(lines) - 30}" if len(lines) > 30 else "")

    def _require_chat(self, chat_id: int) -> Chat:
        chat = self._store.get_chat(chat_id)
        if chat is None:
            raise UserError("Чат не найден.")
        return chat

    def _validate_agent(self, agent: str, model: str, effort: str) -> None:
        spec = next((a for a in self.catalog["agents"] if a["id"] == agent), None)
        if spec is None:
            raise UserError(f"Агент «{agent}» недоступен на этой машине.")
        for value in (model, effort):
            if value and not _SAFE_VALUE.match(value):
                raise UserError("Недопустимое имя модели или уровня.")

    def _label(self, agent: str, model: str) -> str:
        spec = next((a for a in self.catalog["agents"] if a["id"] == agent), {"label": agent, "models": []})
        model_label = next((m["label"] for m in spec["models"] if m["id"] == model), model or "по умолчанию")
        return f"{spec['label']} · {model_label}"
