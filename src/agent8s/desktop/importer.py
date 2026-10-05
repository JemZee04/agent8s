"""Import existing Claude Code sessions (~/.claude/projects/*/*.jsonl) as chats.

A session file is a log of events, not a chat: assistant replies arrive as one
entry per content block, tool results come back as `user` entries, and there
is bookkeeping (titles, snapshots, attachments) in between. This module folds
it into the app's model: one user message per prompt, and one assistant message
per turn holding text and tool parts.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from .drivers import _stringify, clip

CLAUDE_ROOT = Path.home() / ".claude" / "projects"
HEAD_BYTES = 128 * 1024
TAIL_BYTES = 512 * 1024
# Imported history is context, not a live log: keep tool output short so a huge
# session stays pleasant to open (the files on disk are the source of truth).
IMPORT_OUTPUT_LIMIT = 4000
_WRAPPER_TAGS = ("system-reminder", "local-command-caveat", "local-command-stdout", "ide_opened_file", "ide_selection")
_WRAPPER_RE = re.compile(r"<(%s)\b[^>]*>.*?</\1>" % "|".join(_WRAPPER_TAGS), re.S)
_COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>(?:.*?<command-args>(.*?)</command-args>)?", re.S)


@dataclass
class SessionInfo:
    id: str
    path: Path
    title: str
    cwd: str
    branch: str
    mtime: float
    size: int
    first_prompt: str


@dataclass
class ParsedMessage:
    role: str
    parts: list[dict[str, Any]]
    created_at: Optional[str] = None
    model: Optional[str] = None
    status: str = "done"


@dataclass
class ParsedSession:
    title: str
    cwd: str
    branch: str
    messages: list[ParsedMessage] = field(default_factory=list)
    first_at: Optional[str] = None
    last_at: Optional[str] = None


def encode_cwd(cwd: str) -> str:
    """How Claude Code names the project directory for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _events(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def clean_prompt(text: str) -> str:
    """Strip the wrappers Claude Code injects around what the person actually typed."""
    command = _COMMAND_RE.search(text) if "<command-name>" in text else None
    if command:
        return f"{command.group(1).strip()} {(command.group(2) or '').strip()}".strip()
    return _WRAPPER_RE.sub("", text).strip()


def _prompt_text(event: dict[str, Any]) -> Optional[str]:
    """The typed prompt of a `user` event, or None if it is only tool results / bookkeeping."""
    if event.get("isMeta") or event.get("isCompactSummary") or event.get("isSidechain"):
        return None
    content = event.get("message", {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
        if not text and any(isinstance(b, dict) and b.get("type") == "image" for b in content):
            text = "[изображение]"
    else:
        return None
    text = clean_prompt(text)
    return text or None


def _launch_cwd(project_dir: str, cwds: list[str]) -> str:
    """The directory the session was started in. `claude --resume` only finds
    the session from there, and that directory is the one the folder name encodes."""
    for cwd in cwds:
        if encode_cwd(cwd) == project_dir:
            return cwd
    return cwds[0] if cwds else ""


def list_sessions(root: Optional[Path] = None) -> list[SessionInfo]:
    """Cheap metadata for every session: reads only the start and end of each file."""
    root = root or CLAUDE_ROOT
    sessions = []
    if not root.is_dir():
        return sessions
    for path in root.glob("*/*.jsonl"):
        try:
            stat = path.stat()
            head, tail = _read_ends(path, stat.st_size)
        except OSError:
            continue
        cwds, first_prompt, branch = [], "", ""
        for event in _lines(head):
            if event.get("cwd") and event["cwd"] not in cwds:
                cwds.append(event["cwd"])
            branch = branch or event.get("gitBranch", "")
            if not first_prompt and event.get("type") == "user":
                first_prompt = _prompt_text(event) or ""
        title = ""
        for event in _lines(tail):
            if event.get("type") == "custom-title" and event.get("customTitle"):
                title = event["customTitle"]
            elif event.get("type") == "ai-title" and event.get("aiTitle") and not title:
                title = event["aiTitle"]
        sessions.append(
            SessionInfo(
                id=path.stem, path=path, title=title or first_prompt[:80] or path.stem,
                cwd=_launch_cwd(path.parent.name, cwds), branch=branch,
                mtime=stat.st_mtime, size=stat.st_size, first_prompt=first_prompt[:200],
            )
        )
    return sorted(sessions, key=lambda s: s.mtime, reverse=True)


def _read_ends(path: Path, size: int) -> tuple[bytes, bytes]:
    with path.open("rb") as f:
        if size <= HEAD_BYTES + TAIL_BYTES:
            whole = f.read()  # small file: head and tail are the same, complete text
            return whole, whole
        head = f.read(HEAD_BYTES)
        f.seek(size - TAIL_BYTES)
        return head, f.read()


def _lines(blob: bytes) -> Iterator[dict[str, Any]]:
    # A slice of a file starts/ends mid-line; the broken lines simply fail to parse.
    for line in blob.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def parse_session(path: Path) -> ParsedSession:
    cwds: list[str] = []
    branch = ""
    custom_title = ai_title = ""
    messages: list[ParsedMessage] = []
    turn: Optional[ParsedMessage] = None
    tools: dict[str, dict[str, Any]] = {}
    first_at = last_at = None

    for event in _events(path):
        kind = event.get("type")
        if kind == "custom-title":
            custom_title = event.get("customTitle") or custom_title
        elif kind == "ai-title":
            ai_title = event.get("aiTitle") or ai_title
        if kind not in ("user", "assistant") or event.get("isSidechain"):
            continue
        stamp = event.get("timestamp")
        first_at, last_at = first_at or stamp, stamp or last_at
        if event.get("cwd") and event["cwd"] not in cwds:
            cwds.append(event["cwd"])
        branch = event.get("gitBranch") or branch

        if kind == "user":
            if event.get("isCompactSummary"):
                messages.append(ParsedMessage("system", [{"type": "text", "text": "Контекст сессии был сжат (compact)."}], stamp))
                turn = None
                continue
            prompt = _prompt_text(event)
            if prompt is not None:
                messages.append(ParsedMessage("user", [{"type": "text", "text": prompt}], stamp))
                turn = None
                continue
            content = event.get("message", {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    part = tools.get(block.get("tool_use_id", ""))
                    if part is not None:
                        part["output"] = clip(_stringify(block.get("content")), IMPORT_OUTPUT_LIMIT)
                        part["is_error"] = bool(block.get("is_error"))
            continue

        message = event.get("message", {})
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if turn is None:
            turn = ParsedMessage("assistant", [], stamp, message.get("model") or None)
            messages.append(turn)
        if message.get("model"):
            turn.model = message["model"]
        if event.get("isAbortedMidStream"):
            turn.status = "interrupted"
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                if turn.parts and turn.parts[-1]["type"] == "text":
                    turn.parts[-1]["text"] += "\n\n" + block["text"]
                else:
                    turn.parts.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "tool_use" and block.get("id"):
                part = {
                    "type": "tool", "id": block["id"], "name": block.get("name", "tool"),
                    "input": block.get("input", {}), "output": None, "is_error": False, "status": "done",
                }
                tools[block["id"]] = part
                turn.parts.append(part)
            # `thinking` blocks are stored without their text: nothing to show.

    messages = [m for m in messages if m.role != "assistant" or m.parts]
    cwd = _launch_cwd(path.parent.name, cwds)
    first_user = next((m.parts[0]["text"] for m in messages if m.role == "user"), "")
    return ParsedSession(
        title=(custom_title or ai_title or " ".join(first_user.split())[:60] or path.stem)[:120],
        cwd=cwd, branch=branch, messages=messages, first_at=first_at, last_at=last_at,
    )
